import logging
from datetime import datetime
from normality import stringify
from sqlalchemy import or_, not_, func
from itsdangerous import URLSafeTimedSerializer
from werkzeug.security import generate_password_hash, check_password_hash

from aleph.core import db, settings
from aleph.model.common import SoftDeleteModel, IdModel, make_token, query_like
from aleph.util import anonymize_email

log = logging.getLogger(__name__)


membership = db.Table(
    "role_membership",
    db.Column("group_id", db.Integer, db.ForeignKey("role.id")),  # noqa
    db.Column("member_id", db.Integer, db.ForeignKey("role.id")),  # noqa
)


class Role(db.Model, IdModel, SoftDeleteModel):
    """A user, group or other access control subject."""

    __tablename__ = "role"

    USER = "user"
    GROUP = "group"
    SYSTEM = "system"
    TYPES = [USER, GROUP, SYSTEM]

    SYSTEM_GUEST = "guest"
    SYSTEM_USER = "user"

    #: Generates URL-safe signatures for invitations.
    SIGNATURE = URLSafeTimedSerializer(settings.SECRET_KEY)

    #: Signature maximum age, defaults to 1 day
    SIGNATURE_MAX_AGE = 60 * 60 * 24

    foreign_id = db.Column(db.Unicode(2048), nullable=False, unique=True)
    name = db.Column(db.Unicode, nullable=False)
    email = db.Column(db.Unicode, nullable=True)
    type = db.Column(db.Enum(*TYPES, name="role_type"), nullable=False)
    api_key = db.Column(db.Unicode, nullable=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_muted = db.Column(db.Boolean, nullable=False, default=False)
    is_tester = db.Column(db.Boolean, nullable=False, default=False)
    is_blocked = db.Column(db.Boolean, nullable=False, default=False)
    password_digest = db.Column(db.Unicode, nullable=True)
    password = None
    reset_token = db.Column(db.Unicode, nullable=True)
    locale = db.Column(db.Unicode, nullable=True)

    permissions = db.relationship("Permission", backref="role")

    @property
    def has_password(self):
        return self.password_digest is not None

    @property
    def is_public(self):
        return self.id in self.public_roles()

    @property
    def is_actor(self):
        if self.type != self.USER:
            return False
        if self.is_blocked or self.deleted_at is not None:
            return False
        return True

    @property
    def is_alertable(self):
        if self.email is None or not self.is_actor:
            return False
        if self.is_muted:
            return False
        if self.updated_at < (datetime.utcnow() - settings.ROLE_INACTIVE):
            # Disable sending notifications to roles that haven't been
            # logged in for a set amount of time.
            return False
        return True

    @property
    def label(self):
        return anonymize_email(self.name, self.email)

    def update(self, data):
        self.name = data.get("name", self.name)
        if self.name is None:
            self.name = self.email or self.foreign_id
        self.is_muted = data.get("is_muted", self.is_muted)
        self.is_tester = data.get("is_tester", self.is_tester)
        if data.get("password"):
            self.set_password(data.get("password"))
        self.locale = stringify(data.get("locale", self.locale))
        self.touch()

    def touch(self):
        self.updated_at = datetime.utcnow()
        db.session.add(self)

    def clear_roles(self):
        """Removes any existing roles from group membership."""
        self.roles = []
        self.touch()
        db.session.flush()

    def add_role(self, role):
        """Adds an existing role as a membership of a group."""
        self.roles.append(role)
        db.session.add(role)
        db.session.add(self)
        self.updated_at = datetime.utcnow()

    def set_password(self, secret):
        """Hashes and sets the role password.

        :param str secret: The password to be set.
        """
        self.password_digest = generate_password_hash(secret)

    def check_password(self, secret):
        """Checks the password if it matches the role password hash.

        :param str secret: The password to be checked.
        :rtype: bool
        """
        digest = self.password_digest or ""
        return check_password_hash(digest, secret)

    def to_dict(self):
        data = self.to_dict_dates()
        data.update(
            {
                "id": stringify(self.id),
                "type": self.type,
                "name": self.name,
                "label": self.label,
                "email": self.email,
                "locale": self.locale,
                "api_key": self.api_key,
                "is_admin": self.is_admin,
                "is_muted": self.is_muted,
                "is_tester": self.is_tester,
                "has_password": self.has_password,
                # 'notified_at': self.notified_at
            }
        )
        return data

    @classmethod
    def by_foreign_id(cls, foreign_id, deleted=False):
        if foreign_id is not None:
            q = cls.all(deleted=deleted)
            q = q.filter(cls.foreign_id == foreign_id)
            return q.first()

    @classmethod
    def by_email(cls, email):
        if email is None:
            return None
        q = cls.all()
        q = q.filter(func.lower(cls.email) == email.lower())
        q = q.filter(cls.type == cls.USER)
        return q.first()

    @classmethod
    def by_api_key(cls, api_key):
        if api_key is None:
            return None
        q = cls.all()
        q = q.filter_by(api_key=api_key)
        q = q.filter(cls.type == cls.USER)
        q = q.filter(cls.is_blocked == False)  # noqa
        return q.first()

    @classmethod
    def load_or_create(cls, foreign_id, type_, name, email=None, is_admin=False):
        role = cls.by_foreign_id(foreign_id)

        if role is None:
            role = cls()
            role.foreign_id = foreign_id
            role.name = name or email
            role.type = type_
            role.is_admin = is_admin
            role.is_muted = False
            role.is_tester = False
            role.is_blocked = False
            role.notified_at = datetime.utcnow()

        if role.api_key is None:
            role.api_key = make_token()

        if email is not None:
            role.email = email

        db.session.add(role)
        db.session.flush()
        return role

    @classmethod
    def load_cli_user(cls):
        return cls.load_or_create(
            settings.SYSTEM_USER, cls.USER, "Aleph", is_admin=True
        )

    @classmethod
    def load_id(cls, foreign_id):
        """Load a role and return the ID."""
        if not hasattr(settings, "_roles"):
            settings._roles = {}
        if foreign_id not in settings._roles:
            role_id = cls.all_ids().filter_by(foreign_id=foreign_id).first()
            if role_id is not None:
                settings._roles[foreign_id] = role_id[0]
        return settings._roles.get(foreign_id)

    @classmethod
    def public_roles(cls):
        """Roles which make a collection to be considered public."""
        return set([cls.load_id(cls.SYSTEM_USER), cls.load_id(cls.SYSTEM_GUEST)])

    @classmethod
    def by_prefix(cls, prefix, exclude=[]):
        """Load a list of roles matching a name, email address, or foreign_id.

        :param str pattern: Pattern to match.
        """
        q = cls.all_users()
        if len(exclude):
            q = q.filter(not_(Role.id.in_(exclude)))
        q = q.filter(
            or_(func.lower(cls.email) == prefix.lower(), query_like(cls.name, prefix))
        )
        q = q.order_by(Role.id.asc())
        return q

    @classmethod
    def all_groups(cls, authz):
        q = cls.all()
        q = q.filter(Role.type == Role.GROUP)
        q = q.order_by(Role.name.asc())
        q = q.order_by(Role.foreign_id.asc())
        if not authz.is_admin:
            q = q.filter(Role.id.in_(authz.roles))
        return q

    @classmethod
    def all_users(cls):
        q = cls.all().filter(Role.type == Role.USER)
        q = q.filter(cls.is_blocked == False)  # noqa
        return q

    @classmethod
    def all_system(cls):
        return cls.all().filter(Role.type == Role.SYSTEM)

    @classmethod
    def login(cls, email, password):
        """Attempt to log a user in via an email/password method."""
        role = cls.by_email(email)
        if role is None or not role.is_actor or not role.has_password:
            return
        if role.check_password(password):
            return role

    def __repr__(self):
        return "<Role(%r,%r)>" % (self.id, self.foreign_id)


Role.members = db.relationship(
    Role,
    secondary=membership,
    primaryjoin=Role.id == membership.c.group_id,
    secondaryjoin=Role.id == membership.c.member_id,
    backref="roles",
)
