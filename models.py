import uuid
from datetime import datetime, timezone
import enum
from sqlalchemy import Column, String, Float, Text, DateTime, Enum, ForeignKey, Index
from sqlalchemy.orm import relationship
from database import Base

class UserRole(str, enum.Enum):
    USER = "USER"
    DRIVER = "DRIVER"
    HOSPITAL = "HOSPITAL"
    ADMIN = "ADMIN"

class RequestStatus(str, enum.Enum):
    REQUESTED = "REQUESTED"
    ASSIGNED = "ASSIGNED"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

class AmbulanceStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    email = Column(String(120), unique=True, nullable=False, index=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    passwordHash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), default=UserRole.USER, nullable=False)
    createdAt = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updatedAt = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    requests = relationship("EmergencyRequest", back_populates="user", cascade="all, delete-orphan")
    ambulances = relationship("Ambulance", back_populates="driver")

    def to_dict(self):
        """Returns safe user representation excluding passwordHash."""
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "role": self.role.value if isinstance(self.role, UserRole) else str(self.role),
            "createdAt": self.createdAt.isoformat() if self.createdAt else None,
            "updatedAt": self.updatedAt.isoformat() if self.updatedAt else None
        }


class Ambulance(Base):
    __tablename__ = "ambulances"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    vehicle_number = Column(String(50), unique=True, nullable=False, index=True)
    ambulance_type = Column(String(50), default="ALS", nullable=False, index=True)
    status = Column(Enum(AmbulanceStatus), default=AmbulanceStatus.AVAILABLE, nullable=False, index=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    speed = Column(Float, nullable=True)
    heading = Column(Float, nullable=True)
    location_updated_at = Column(DateTime, nullable=True)
    driver_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    driver = relationship("User", back_populates="ambulances")
    requests = relationship("EmergencyRequest", back_populates="assigned_ambulance")

    def to_dict(self):
        driver_obj = self.driver
        return {
            "id": self.id,
            "vehicle_number": self.vehicle_number,
            "ambulance_type": self.ambulance_type,
            "status": self.status.value if isinstance(self.status, AmbulanceStatus) else str(self.status),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "speed": self.speed,
            "heading": self.heading,
            "location_updated_at": self.location_updated_at.isoformat() if self.location_updated_at else None,
            "driver_id": self.driver_id,
            "driver_name": driver_obj.name if driver_obj else "Unassigned",
            "driver_phone": driver_obj.phone if driver_obj else "N/A",
            "driver": {
                "id": driver_obj.id,
                "name": driver_obj.name,
                "phone": driver_obj.phone,
                "email": driver_obj.email
            } if driver_obj else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class EmergencyRequest(Base):
    __tablename__ = "emergency_requests"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    assigned_ambulance_id = Column(String(36), ForeignKey("ambulances.id"), nullable=True, index=True)
    emergency_type = Column(String(50), nullable=False)
    ambulance_type = Column(String(50), default="ALS", nullable=False)
    pickup_address = Column(String(255), nullable=False)
    pickup_latitude = Column(Float, nullable=True)
    pickup_longitude = Column(Float, nullable=True)
    additional_notes = Column(Text, nullable=True)
    status = Column(Enum(RequestStatus), default=RequestStatus.REQUESTED, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", back_populates="requests")
    assigned_ambulance = relationship("Ambulance", back_populates="requests")
    alerts = relationship("EmergencyAlert", back_populates="request", cascade="all, delete-orphan")

    def to_dict(self):
        data = {
            "id": self.id,
            "user_id": self.user_id,
            "assigned_ambulance_id": self.assigned_ambulance_id,
            "emergency_type": self.emergency_type,
            "ambulance_type": self.ambulance_type,
            "pickup_address": self.pickup_address,
            "pickup_latitude": self.pickup_latitude,
            "pickup_longitude": self.pickup_longitude,
            "additional_notes": self.additional_notes,
            "status": self.status.value if isinstance(self.status, RequestStatus) else str(self.status),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }
        if self.assigned_ambulance:
            drv = self.assigned_ambulance.driver
            data["assigned_ambulance"] = {
                "id": self.assigned_ambulance.id,
                "vehicle_number": self.assigned_ambulance.vehicle_number,
                "ambulance_type": self.assigned_ambulance.ambulance_type,
                "status": self.assigned_ambulance.status.value if isinstance(self.assigned_ambulance.status, AmbulanceStatus) else str(self.assigned_ambulance.status),
                "latitude": self.assigned_ambulance.latitude,
                "longitude": self.assigned_ambulance.longitude,
                "speed": self.assigned_ambulance.speed,
                "heading": self.assigned_ambulance.heading,
                "location_updated_at": self.assigned_ambulance.location_updated_at.isoformat() if self.assigned_ambulance.location_updated_at else None,
                "driver_id": self.assigned_ambulance.driver_id,
                "driver_name": drv.name if drv else "On-Duty Paramedic",
                "driver_phone": drv.phone if drv else "+1 (555) 987-6543",
                "driver": {
                    "id": drv.id,
                    "name": drv.name,
                    "phone": drv.phone,
                    "email": drv.email
                } if drv else None
            }
        else:
            data["assigned_ambulance"] = None

        return data


class Hospital(Base):
    __tablename__ = "hospitals"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String(150), nullable=False, index=True)
    address = Column(String(255), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    phone = Column(String(25), nullable=False)
    trauma_level = Column(String(50), default="Level 1 Trauma", nullable=False)
    icu_beds_available = Column(Float, default=12, nullable=False)
    emergency_capacity = Column(String(50), default="NORMAL", nullable=False)
    pincode = Column(String(20), nullable=True, index=True)
    city = Column(String(100), nullable=True, index=True)
    taluka = Column(String(100), nullable=True)
    district = Column(String(100), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "address": self.address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "phone": self.phone,
            "trauma_level": self.trauma_level,
            "icu_beds_available": int(self.icu_beds_available) if self.icu_beds_available is not None else 0,
            "emergency_capacity": self.emergency_capacity,
            "pincode": self.pincode or "",
            "city": self.city or "",
            "taluka": self.taluka or "",
            "district": self.district or "",
            "is_registered": True,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class EmergencyAlert(Base):
    __tablename__ = "emergency_alerts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id = Column(String(36), ForeignKey("emergency_requests.id"), nullable=False, index=True)
    recipient_type = Column(String(20), nullable=False, index=True)  # "AMBULANCE", "HOSPITAL"
    recipient_id = Column(String(36), nullable=False, index=True)    # ambulance_id or hospital_id
    recipient_name = Column(String(150), nullable=False)
    recipient_phone = Column(String(50), nullable=True)              # phone number of recipient
    alert_type = Column(String(50), default="DISPATCH_NOTIFICATION", nullable=False)  # PRIMARY_DISPATCH, NEARBY_STANDBY, HOSPITAL_INCOMING_ALERT
    distance_km = Column(Float, nullable=True)
    eta_minutes = Column(Float, nullable=True)
    message = Column(Text, nullable=False)
    is_read = Column(DateTime, nullable=True)  # timestamp when acknowledged/read
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    request = relationship("EmergencyRequest", back_populates="alerts")

    def to_dict(self):
        return {
            "id": self.id,
            "request_id": self.request_id,
            "recipient_type": self.recipient_type,
            "recipient_id": self.recipient_id,
            "recipient_name": self.recipient_name,
            "recipient_phone": self.recipient_phone,
            "alert_type": self.alert_type,
            "distance_km": self.distance_km,
            "eta_minutes": self.eta_minutes,
            "message": self.message,
            "is_read": self.is_read.isoformat() if self.is_read else None,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class NotificationDeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"


class EcosystemNotification(Base):
    __tablename__ = "ecosystem_notifications"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type = Column(String(50), nullable=False, index=True)
    request_id = Column(String(36), ForeignKey("emergency_requests.id"), nullable=True, index=True)
    recipient_role = Column(String(20), nullable=False, index=True)  # DRIVER, HOSPITAL, ADMIN, USER
    recipient_user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    recipient_ambulance_id = Column(String(36), ForeignKey("ambulances.id"), nullable=True, index=True)
    recipient_hospital_id = Column(String(36), ForeignKey("hospitals.id"), nullable=True, index=True)
    title = Column(String(150), nullable=False)
    message = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=True)
    delivery_status = Column(String(20), default="PENDING", nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    delivered_at = Column(DateTime, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    retry_count = Column(Float, default=0, nullable=False)
    last_error = Column(Text, nullable=True)

    request = relationship("EmergencyRequest")
    recipient_user = relationship("User", foreign_keys=[recipient_user_id])
    recipient_ambulance = relationship("Ambulance", foreign_keys=[recipient_ambulance_id])
    recipient_hospital = relationship("Hospital", foreign_keys=[recipient_hospital_id])

    def to_dict(self):
        import json
        payload = {}
        if self.payload_json:
            try:
                payload = json.loads(self.payload_json)
            except Exception:
                payload = {}
        return {
            "id": self.id,
            "event_type": self.event_type,
            "request_id": self.request_id,
            "recipient_role": self.recipient_role,
            "recipient_user_id": self.recipient_user_id,
            "recipient_ambulance_id": self.recipient_ambulance_id,
            "recipient_hospital_id": self.recipient_hospital_id,
            "title": self.title,
            "message": self.message,
            "payload": payload,
            "delivery_status": self.delivery_status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "retry_count": int(self.retry_count) if self.retry_count is not None else 0,
            "last_error": self.last_error
        }


