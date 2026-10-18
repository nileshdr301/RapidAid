import sys
from werkzeug.security import generate_password_hash
from database import init_db, db_session
from models import User, UserRole, Ambulance, AmbulanceStatus

def seed():
    print("Initializing database tables...")
    init_db()

    # Seed demo user
    demo_user_email = "user@rapidaid.org"
    demo_user_phone = "+15551234567"
    existing_user = db_session.query(User).filter((User.email == demo_user_email) | (User.phone == demo_user_phone)).first()
    if not existing_user:
        user = User(
            name="John Doe",
            email=demo_user_email,
            phone=demo_user_phone,
            passwordHash=generate_password_hash("Password123!"),
            role=UserRole.USER
        )
        db_session.add(user)
        db_session.flush()
        print(f"Created demo user: {demo_user_email}")

    # Seed demo drivers
    drivers_data = [
        {"name": "Marcus Vance", "email": "driver@rapidaid.org", "phone": "+15559876543"},
        {"name": "Sarah Jenkins", "email": "sarah.driver@rapidaid.org", "phone": "+15552345678"},
        {"name": "David Miller", "email": "david.driver@rapidaid.org", "phone": "+15553456789"},
        {"name": "Robert Chen", "email": "robert.driver@rapidaid.org", "phone": "+15554567890"},
        {"name": "Elena Gomez", "email": "elena.driver@rapidaid.org", "phone": "+15555678901"}
    ]
    created_drivers = {}
    for d in drivers_data:
        drv = db_session.query(User).filter((User.email == d["email"]) | (User.phone == d["phone"])).first()
        if not drv:
            drv = User(
                name=f"{d['name']} (Driver)",
                email=d["email"],
                phone=d["phone"],
                passwordHash=generate_password_hash("Password123!"),
                role=UserRole.DRIVER
            )
            db_session.add(drv)
            db_session.flush()
            print(f"Created demo driver: {d['email']} ({d['phone']})")
        created_drivers[d["email"]] = drv

    driver = created_drivers.get("driver@rapidaid.org")

    # Seed master administrator (Nilesh Raut)
    master_admin_email = "nileshdr301@gmail.com"
    master_admin_phone = "+919876543210"
    existing_master_admin = db_session.query(User).filter_by(email=master_admin_email).first()
    if not existing_master_admin:
        master_admin = User(
            name="Nilesh Raut (Master Administrator)",
            email=master_admin_email,
            phone=master_admin_phone,
            passwordHash=generate_password_hash("Nilesh@301IIT"),
            role=UserRole.ADMIN
        )
        db_session.add(master_admin)
        print(f"Created master admin: {master_admin_email}")
    else:
        existing_master_admin.passwordHash = generate_password_hash("Nilesh@301IIT")
        existing_master_admin.role = UserRole.ADMIN

    # Seed legacy test admin for test suites
    demo_admin_email = "admin@rapidaid.org"
    demo_admin_phone = "+15550001122"
    existing_admin = db_session.query(User).filter((User.email == demo_admin_email) | (User.phone == demo_admin_phone)).first()
    if not existing_admin:
        admin = User(
            name="System Admin",
            email=demo_admin_email,
            phone=demo_admin_phone,
            passwordHash=generate_password_hash("Password123!"),
            role=UserRole.ADMIN
        )
        db_session.add(admin)
        print(f"Created demo admin: {demo_admin_email}")

    # Seed demo hospital
    demo_hospital_email = "hospital@rapidaid.org"
    demo_hospital_phone = "7620781583"
    hospital_user = db_session.query(User).filter((User.email == demo_hospital_email) | (User.phone == demo_hospital_phone)).first()
    if not hospital_user:
        hospital_user = User(
            name="Government Hospital of Indapur (ER Command)",
            email=demo_hospital_email,
            phone=demo_hospital_phone,
            passwordHash=generate_password_hash("Password123!"),
            role=UserRole.HOSPITAL
        )
        db_session.add(hospital_user)
        db_session.flush()
        print(f"Created demo hospital: {demo_hospital_email}")
    else:
        hospital_user.name = "Government Hospital of Indapur (ER Command)"
        hospital_user.phone = demo_hospital_phone
        hospital_user.role = UserRole.HOSPITAL

    # Seed Deterministic Development Ambulances
    seed_ambulances = [
        {"vehicle_number": "AMB-101", "ambulance_type": "ALS", "status": AmbulanceStatus.AVAILABLE, "latitude": 37.7750, "longitude": -122.4190, "driver_id": created_drivers.get("driver@rapidaid.org").id if created_drivers.get("driver@rapidaid.org") else None},
        {"vehicle_number": "AMB-102", "ambulance_type": "ALS", "status": AmbulanceStatus.AVAILABLE, "latitude": 37.7900, "longitude": -122.4000, "driver_id": created_drivers.get("sarah.driver@rapidaid.org").id if created_drivers.get("sarah.driver@rapidaid.org") else None},
        {"vehicle_number": "AMB-103", "ambulance_type": "ALS", "status": AmbulanceStatus.BUSY, "latitude": 37.7749, "longitude": -122.4194, "driver_id": None},
        {"vehicle_number": "AMB-104", "ambulance_type": "ALS", "status": AmbulanceStatus.OFFLINE, "latitude": 37.7749, "longitude": -122.4194, "driver_id": None},
        {"vehicle_number": "AMB-105", "ambulance_type": "ICU", "status": AmbulanceStatus.AVAILABLE, "latitude": 37.7760, "longitude": -122.4180, "driver_id": created_drivers.get("david.driver@rapidaid.org").id if created_drivers.get("david.driver@rapidaid.org") else None},
        {"vehicle_number": "AMB-106", "ambulance_type": "BLS", "status": AmbulanceStatus.AVAILABLE, "latitude": 37.7770, "longitude": -122.4170, "driver_id": created_drivers.get("robert.driver@rapidaid.org").id if created_drivers.get("robert.driver@rapidaid.org") else None},
        {"vehicle_number": "AMB-107", "ambulance_type": "NEONATAL", "status": AmbulanceStatus.AVAILABLE, "latitude": 37.7780, "longitude": -122.4160, "driver_id": created_drivers.get("elena.driver@rapidaid.org").id if created_drivers.get("elena.driver@rapidaid.org") else None},
    ]

    for amb_data in seed_ambulances:
        existing_amb = db_session.query(Ambulance).filter_by(vehicle_number=amb_data["vehicle_number"]).first()
        if not existing_amb:
            amb = Ambulance(**amb_data)
            db_session.add(amb)
            print(f"Created seed ambulance: {amb_data['vehicle_number']} ({amb_data['ambulance_type']}) - {amb_data['status'].value}")
        else:
            existing_amb.status = amb_data["status"]
            existing_amb.driver_id = amb_data["driver_id"]
            existing_amb.latitude = amb_data["latitude"]
            existing_amb.longitude = amb_data["longitude"]
            print(f"Updated seed ambulance: {existing_amb.vehicle_number} status -> {existing_amb.status.value}")

    # Seed Deterministic Hospitals (Currently only Government Hospital of Indapur)
    from models import Hospital
    seed_hospitals = [
        {
            "name": "Government Hospital of Indapur",
            "address": "Indapur, Taluka Indapur, Dist Pune",
            "city": "Indapur",
            "taluka": "Indapur",
            "district": "Pune",
            "pincode": "413106",
            "latitude": 18.1171,
            "longitude": 75.0305,
            "phone": "7620781583",
            "trauma_level": "Government Multi-Specialty & Rural Trauma Center",
            "icu_beds_available": 12,
            "emergency_capacity": "NORMAL",
            "user_id": hospital_user.id if hospital_user else None
        }
    ]

    # Delete existing hospitals to ensure only the currently registered hospital is active
    db_session.query(Hospital).delete()
    for hosp_data in seed_hospitals:
        hosp = Hospital(**hosp_data)
        db_session.add(hosp)
        print(f"Created seed hospital: {hosp_data['name']} ({hosp_data['trauma_level']}) - Phone: {hosp_data['phone']}")

    db_session.commit()
    print("Database seeding completed successfully.")

if __name__ == "__main__":
    seed()
