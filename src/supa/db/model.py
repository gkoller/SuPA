#  Copyright 2020 SURF.
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Define the database models.

Surrogate keys versus natural keys
==================================

Looking at the model definitions
you'll find that we have used `natural keys <https://en.wikipedia.org/wiki/Natural_key>`_ wherever possible.
Though no too common these days,
with the prevalent use of ORMs
that automatically generate a `surrogate key <https://en.wikipedia.org/wiki/Surrogate_key>`_ per model,
SQLAlchemy is flexible enough to model things 'naturally' from a relational database point of view.
This sometimes results in `composite <https://en.wikipedia.org/wiki/Compound_key>`_ primary keys.

Foreign keys to these composite primary keys cannot be defined on a specific Column definition
or even a set of Column definitions.
Something that does work for composite primary key definitions.
Instead,
these foreign keys need to be defined using a
`ForeignKeyConstraint <https://docs.sqlalchemy.org/en/13/core/constraints.html?sqlalchemy.schema.ForeignKeyConstraint#sqlalchemy.schema.ForeignKeyConstraint>`_
on the ``__table_args__`` attribute of the DB model.

Connection IDs
==============

Looking at example messages in the different specifications:

* `GFD-R-233 Applying Policy in the NSI Environment (pdf) <https://www.ogf.org/documents/GFD.233.pdf>`_
* `GWD-R-P.237 NSI Connection Service v2.1 (pdf) <https://www.ogf.org/documents/GFD.237.pdf>`_

we see that connection IDs always seem to be formatted as ``UUID``'s.
However, according to its definition in GWD-R-P.237,
it can be any string as long as it is unique within the context of a PA.
That is the reason that we have modelled connection IDs from other NSA's
(``ag_connection_id``, ``upa_connection_id``)
as ``TEXT``.
Within SuPA we have decided to use ``UUID``'s for our ``connection_id``'s.


SQLAlchemy Model Dependency Diagram
===================================

A visual representation of how everything is wired together
should help navigating the Python code a lot better.

.. image:: /images/sqlalchemy_model_dependency_diagram.png


"""  # noqa: E501 B950
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    TypeDecorator,
    UniqueConstraint,
    inspect,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import DontWrapMixin
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.orderinglist import ordering_list
from sqlalchemy.orm import relationship
from sqlalchemy.orm.state import InstanceState

from supa.connection.fsm import LifecycleStateMachine, ProvisioningStateMachine, ReservationStateMachine
from supa.util import nsi
from supa.util.timestamp import NO_END_DATE, current_timestamp


class Uuid(TypeDecorator):
    """Implement SQLAlchemy Uuid column type for SQLite databases.

    This stores Python :class:`uuid.UUID` types as strings (``CHAR(36)``) in the database.
    We have chosen to store the :meth:`uuid.UUID.__str__` representation directly,
    eg. with ``"-"`` between the UUID fields,
    for improved readability.
    """

    impl = sqlite.CHAR(36)

    def process_bind_param(self, value: Optional[uuid.UUID], dialect: Dialect) -> Optional[str]:  # noqa: D102
        if value is not None:
            if not isinstance(value, uuid.UUID):
                raise ValueError(f"'{value}' is not a valid UUID.")
            return str(value)
        return value

    def process_result_value(self, value: Optional[str], dialect: Dialect) -> Optional[uuid.UUID]:  # noqa: D102
        if value is None:
            return value
        return uuid.UUID(value)


class ReprBase:
    """Custom SQLAlchemy model to provide meaningful :meth:`__str__` and :meth:`__repr__` methods.

    Writing appropriate ``__repr__`` and ``__str__`` methods
    for all your SQLAlchemy ORM models
    gets tedious very quickly.
    By using SQLAlchemy's
    `Runtime Inspection API
    <https://docs.sqlalchemy.org/en/latest/core/inspection.html?highlight=runtime inspection api>`_
    this base class can easily generate these methods for you.

    .. note:: This class cannot be used as a regular Python base class
              due to assumptions made by ``declarative_base``. See **Usage** below instead.

    Usage::

        Base = declarative_base(cls=ReprBase)
    """

    def __repr__(self) -> str:
        """Return string that represents a SQLAlchemy ORM model."""
        inst_state: InstanceState = inspect(self)
        attr_vals = [f"{attr.key}={getattr(self, attr.key)}" for attr in inst_state.mapper.column_attrs]
        return f"{self.__class__.__name__}({', '.join(attr_vals)})"

    def __str__(self) -> str:
        """Return string that represents a SQLAlchemy ORM model."""
        return self.__repr__()


class UtcTimestampException(Exception, DontWrapMixin):
    """Exception class for custom UtcTimestamp SQLAlchemy column type."""

    pass


class UtcTimestamp(TypeDecorator):
    """Custom SQLAlchemy column type for storing timestamps in UTC in SQLite databases.

    This column type always returns timestamps with the UTC timezone.
    It also guards against accidentally trying to store Python naive timestamps
    (those without a time zone).

    In the SQLite database the timestamps are stored as strings of format: ``yyyy-mm-dd hh:mm:ss``.
    **UTC is always implied.**
    """

    impl = sqlite.DATETIME(truncate_microseconds=True)

    def process_bind_param(self, value: Optional[datetime], dialect: Dialect) -> Optional[datetime]:  # noqa: D102
        if value is not None:
            if value.tzinfo is None:
                raise UtcTimestampException(f"Expected timestamp with tzinfo. Got naive timestamp {value!r} instead")
            return value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value: Optional[datetime], dialect: Dialect) -> Optional[datetime]:  # noqa: D102
        if value is not None:
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc)
            return value.replace(tzinfo=timezone.utc)
        return value


# Using type ``Any`` because: https://github.com/python/mypy/issues/2477
Base: Any = declarative_base(cls=ReprBase)


class Reservation(Base):
    """DB mapping for registering NSI reservations."""

    __tablename__ = "reservations"

    # Most of these attribute come from different parts of the ``ReserveRequest`` message.
    # Although this is not a direct mapping, we have indicated from what parts some these
    # attribute comes from.

    connection_id = Column(Uuid, primary_key=True, default=uuid.uuid4)

    # header
    protocol_version = Column(Text, nullable=False)
    correlation_id = Column(Uuid, nullable=False, comment="urn:uid", unique=True)
    requester_nsa = Column(Text, nullable=False)
    provider_nsa = Column(Text, nullable=False)
    reply_to = Column(Text)
    session_security_attributes = Column(Text)

    # request message (+ connection_id)
    global_reservation_id = Column(Text, nullable=False)
    description = Column(Text)

    # reservation request criteria
    version = Column(Integer, nullable=False)

    # schedule
    start_time = Column(UtcTimestamp, nullable=False, default=current_timestamp, index=True)
    end_time = Column(UtcTimestamp, nullable=False, default=NO_END_DATE, index=True)

    # p2p
    bandwidth = Column(Integer, nullable=False, comment="Mbps")
    directionality = Column(Enum("BI_DIRECTIONAL", "UNI_DIRECTIONAL"), nullable=False, default="BI_DIRECTIONAL")
    symmetric = Column(Boolean, nullable=False)

    src_domain = Column(Text, nullable=False)
    src_network_type = Column(Text, nullable=False)
    src_port = Column(Text, nullable=False, comment="Name of the port")
    src_vlans = Column(Text, nullable=False)

    # `src_vlans` might be a range of VLANs in case the reservation specified an unqualified STP.
    # In that case it is up to the reservation process to select an available VLAN out of the
    # supplied range.
    # This also explain the difference in column types. A range is expressed as a string (eg "1-10").
    # A single VLAN is always a single number, hence integer.
    src_selected_vlan = Column(Integer, nullable=True)
    dst_domain = Column(Text, nullable=False)
    dst_network_type = Column(Text, nullable=False)
    dst_port = Column(Text, nullable=False, comment="Name of the port")
    dst_vlans = Column(Text, nullable=False)

    # See `src_selected_vlan`
    dst_selected_vlan = Column(Integer, nullable=True)

    # internal state keeping
    reservation_state = Column(
        Enum(*[s.value for s in ReservationStateMachine.states]),
        nullable=False,
        default=ReservationStateMachine.ReserveStart.value,  # type: ignore[has-type]
    )
    provisioning_state = Column(Enum(*[s.name for s in ProvisioningStateMachine.states]))
    lifecycle_state = Column(
        Enum(*[s.value for s in LifecycleStateMachine.states]),
        nullable=False,
        default=LifecycleStateMachine.Created.value,  # type: ignore[has-type]
    )

    # another header part
    path_trace = relationship(
        "PathTrace",
        uselist=False,
        back_populates="reservation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )  # one-to-one

    parameters = relationship(
        "Parameter",
        backref="reservation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    connection = relationship(
        "Connection",
        uselist=False,
        back_populates="reservation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )  # one-to-one

    __table_args__ = (CheckConstraint(start_time < end_time),)

    def src_stp(self, selected: bool = False) -> nsi.Stp:
        """Return :class:`~supa.util.nsi.STP` instance for src data.

        Depending on where we are in the reservation process,
        we need to deal with a requested VLAN(s)(ranges),
        or a selected VLAN.
        The ``selected`` parameter determines which of the two
        will be used for the ``labels`` argument to the :class:`~supa.util.nsi.Stp` object.

        Args:
            selected: if True, use 'selected VLAN` instead of requested VLAN(s)(ranges)

        Returns:
            :class:`~supa.util.nsi.Stp` object
        """
        vlans = self.src_selected_vlan if selected else self.src_vlans
        labels = f"vlan={vlans}"
        return nsi.Stp(self.src_domain, self.src_network_type, self.src_port, labels)

    def dst_stp(self, selected: bool = False) -> nsi.Stp:
        """Return :class:`~supa.util.nsi.STP` instance for dst data.

        Depending on where we are in the reservation process,
        we need to deal with a requested VLAN(s)(ranges),
        or a selected VLAN.
        The ``selected`` parameter determines which of the two
        will be used for the ``labels`` argument to the :class:`~supa.util.nsi.Stp` object.

        Args:
            selected: if True, use 'selected VLAN` instead of requested VLAN(s)(ranges)

        Returns:
            :class:`~supa.util.nsi.Stp` object
        """
        vlans = self.dst_selected_vlan if selected else self.dst_vlans
        labels = f"vlan={vlans}"
        return nsi.Stp(self.dst_domain, self.dst_network_type, self.dst_port, labels)


class PathTrace(Base):
    """DB mapping for PathTraces."""

    __tablename__ = "path_traces"

    path_trace_id = Column(Text, primary_key=True, comment="NSA identifier of root or head-end aggregator NSA")
    ag_connection_id = Column(Text, primary_key=True, comment="Aggregator issued connection_id")

    connection_id = Column(
        Uuid, ForeignKey(Reservation.connection_id, ondelete="CASCADE"), nullable=False, comment="Our connection_id"
    )

    reservation = relationship(
        Reservation,
        back_populates="path_trace",
    )  # one-to-one (cascades defined in parent)

    paths = relationship(
        "Path",
        backref="path_trace",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Ensure that the column used in joins with the parent table will have an index.
        Index("fx_to_reservations_idx", connection_id),
    )


class Path(Base):
    """DB mapping for Paths."""

    __tablename__ = "paths"

    path_id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    path_trace_id = Column(Text, nullable=False)
    ag_connection_id = Column(Text, nullable=False)

    segments = relationship(
        "Segment",
        backref="path",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Segment.order",
        collection_class=ordering_list("order"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            (path_trace_id, ag_connection_id), (PathTrace.path_trace_id, PathTrace.ag_connection_id), ondelete="CASCADE"
        ),
        # Ensure that columns used in joins with the parent table will have an index.
        Index("fk_to_path_traces_idx", path_trace_id, ag_connection_id),
    )


class Segment(Base):
    """DB mapping for Segment."""

    __tablename__ = "segments"

    segment_id = Column(
        Text, primary_key=True, comment="The NSA identifier for the uPA associated with this path segment"
    )
    path_id = Column(Uuid, ForeignKey(Path.path_id, ondelete="CASCADE"), nullable=False, primary_key=True)

    upa_connection_id = Column(Text, nullable=False, comment="Not ours; it's is the connection_id from another uPA")
    order = Column(Integer, nullable=False)

    stps = relationship(
        "Stp",
        backref="segment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Stp.order",
        collection_class=ordering_list("order"),
    )

    # By virtue of the composite unique constraint,
    # a composite index will be created with the first column being `path_id`.
    # This index can be for joins involving the foreign key column.
    # Hence no need to create a separate index
    __table_args__ = (UniqueConstraint(path_id, order),)


class Stp(Base):
    """DB Mapping for STP."""

    __tablename__ = "stps"

    stp_id = Column(Text, primary_key=True, comment="Assumes fully qualified STP")
    segment_id = Column(Text, nullable=False)
    path_id = Column(Uuid, nullable=False)
    order = Column(Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint((segment_id, path_id), (Segment.segment_id, Segment.path_id), ondelete="CASCADE"),
        UniqueConstraint(segment_id, order),
        # Ensure that columns used in joins with the parent table will have an index.
        Index("fk_to_segment_idx", segment_id, path_id),
    )


class Parameter(Base):
    """DB mapping for PointToPointService Parameters."""

    __tablename__ = "parameters"

    connection_id = Column(
        Uuid, ForeignKey(Reservation.connection_id, ondelete="CASCADE"), nullable=False, primary_key=True
    )
    key = Column(Text, primary_key=True)
    value = Column(Text)


class Port(Base):
    """DB mapping for ports (STPs) from the Orchestrator."""

    __tablename__ = "ports"

    port_id = Column(Uuid, primary_key=True, comment="subscription_id of a port in the Orchestrator")
    name = Column(Text, nullable=False, unique=True, index=True)
    vlans = Column(Text, nullable=False)
    remote_stp = Column(Text, nullable=True)  # not sure if we need this?
    bandwidth = Column(Integer, nullable=False, comment="Mbps")

    # A port might still be in operation (eg active) as part of one or more connections.
    # However to prevent new reservations be made against it,
    # we can enable of disable it.
    enabled = Column(Boolean, nullable=False, default=True, comment="We don't delete ports, we enable or disable them.")


class Connection(Base):
    """DB mapping for registering connections to be build/built.

    It stores references to the actual :class`Port`s used in the  connection
    and the ``subscription_id`` of the lightpath from the Orchestrator.
    """

    __tablename__ = "connections"

    connection_id = Column(Uuid, ForeignKey(Reservation.connection_id, ondelete="CASCADE"), primary_key=True)
    bandwidth = Column(Integer, nullable=False, comment="Mbps")

    # Mind the singular {src,dst}_vlan
    # compared to the plural {src,dst}_vlans in
    # :class:`Reservation`.
    # We use singular here,
    # as by the time we are creating a Connection a VLAN
    # per port will have been selected.
    source_port_id = Column(Uuid, ForeignKey(Port.port_id), nullable=False)
    source_vlan = Column(Integer, nullable=False)
    dest_port_id = Column(Uuid, ForeignKey(Port.port_id), nullable=False)
    dest_vlan = Column(Integer, nullable=False)
    subscription_id = Column(
        Uuid, nullable=False, unique=True, comment="subscription_id of the lightpath in the Orchestrator"
    )

    reservation = relationship(
        Reservation,
        back_populates="connection",
    )  # one-to-one (cascades defined in parent)

    source_port = relationship(
        "Port",
        foreign_keys=[source_port_id],
    )

    dest_port = relationship(
        "Port",
        foreign_keys=[dest_port_id],
    )
