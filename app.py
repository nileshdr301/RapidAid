import os
import re
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, Response
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

import json
import time
import queue

from database import db_session, init_db
from models import User, UserRole, Ambulance, AmbulanceStatus, EmergencyRequest, RequestStatus, Hospital, EmergencyAlert, EcosystemNotification
from services.matching import find_and_assign_ambulance, calculate_haversine_distance, calculate_ambulance_eta_minutes
from services.notifications import broadcast_emergency_alerts
from services.eta import calculate_eta
from services.event_bus import event_bus

from sqlalchemy import text

# Load environment variables
load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "rapidaid_fallback_secret_key_2026")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.getenv("FLASK_ENV") == "production" or os.getenv("SECURE_COOKIES", "").lower() == "true":
    app.config["SESSION_COOKIE_SECURE"] = True

ALLOWED_EMERGENCY_TYPES = {"cardiac", "trauma", "maternity", "general", "ic_care"}
ALLOWED_AMBULANCE_TYPES = {"ALS", "BLS", "NEONATAL", "ICU"}

# Security headers middleware
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# Teardown database session after each request
@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()

# Initialize DB on startup
with app.app_context():
    init_db()

# --- Health Check Endpoint ---
@app.route("/api/health", methods=["GET"])
def health_check():
    try:
        db_session.execute(text("SELECT 1"))
        return jsonify({
            "status": "healthy",
            "database": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 200
    except Exception as e:
        app.logger.error(f"Health check database failure: {str(e)}")
        return jsonify({
            "status": "degraded",
            "database": "unavailable",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 503


# --- Helper Validation & Auth Functions ---
def is_valid_email(email):
    pattern = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    return re.match(pattern, email) is not None

def is_valid_phone(phone):
    cleaned = re.sub(r"[^\d+]", "", phone)
    return len(cleaned) >= 7

def get_authenticated_user_id():
    return session.get("user_id")

def get_authenticated_user():
    user_id = get_authenticated_user_id()
    if not user_id:
        return None
    return db_session.get(User, user_id)

def driver_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_authenticated_user()
        if not user:
            return jsonify({"error": "Authentication required."}), 401
        if user.role != UserRole.DRIVER and user.role != UserRole.ADMIN:
            return jsonify({"error": "Access denied. Driver privileges required."}), 403
        return f(user, *args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_authenticated_user()
        if not user:
            return jsonify({"error": "Authentication required."}), 401
        if user.role != UserRole.ADMIN:
            return jsonify({"error": "Access denied. Admin privileges required."}), 403
        return f(user, *args, **kwargs)
    return decorated_function

def hospital_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_authenticated_user()
        if not user:
            return jsonify({"error": "Authentication required."}), 401
        if user.role != UserRole.HOSPITAL and user.role != UserRole.ADMIN:
            return jsonify({"error": "Access denied. Hospital privileges required."}), 403
        return f(user, *args, **kwargs)
    return decorated_function

# --- Authentication API Routes ---

@app.route("/api/auth/register", methods=["POST"])
def register():
    try:
        data = request.get_json() or {}
        name = data.get("name", "").strip()
        email = data.get("email", "").strip().lower()
        phone = data.get("phone", "").strip()
        password = data.get("password", "")
        role_str = data.get("role", "USER").strip().upper()

        if not name:
            return jsonify({"error": "Full name / Hospital name is required."}), 400
        if not email or not is_valid_email(email):
            return jsonify({"error": "A valid email address is required."}), 400
        if not phone or not is_valid_phone(phone):
            return jsonify({"error": "A valid phone number is required."}), 400
        if not password or len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters long."}), 400

        if email == "nileshdr301@gmail.com":
            return jsonify({"error": "This email is reserved for the Master System Administrator."}), 403

        if role_str == "ADMIN":
            return jsonify({"error": "Administrator registration is disabled. Contact system authorities."}), 403

        try:
            target_role = UserRole(role_str)
            if target_role == UserRole.ADMIN:
                target_role = UserRole.USER
        except ValueError:
            target_role = UserRole.USER

        if db_session.query(User).filter_by(email=email).first():
            return jsonify({"error": "An account with this email already exists."}), 409
        if db_session.query(User).filter_by(phone=phone).first():
            return jsonify({"error": "An account with this phone number already exists."}), 409

        hashed_password = generate_password_hash(password)
        new_user = User(
            name=name,
            email=email,
            phone=phone,
            passwordHash=hashed_password,
            role=target_role
        )
        db_session.add(new_user)
        db_session.flush()

        # If registering as DRIVER, automatically link an ambulance
        if target_role == UserRole.DRIVER:
            vehicle_number = data.get("vehicle_number", "").strip().upper()
            ambulance_type = data.get("ambulance_type", "ALS").strip().upper()
            if not vehicle_number:
                vehicle_number = f"AMB-{new_user.id[:5].upper()}"
            if ambulance_type not in ALLOWED_AMBULANCE_TYPES:
                ambulance_type = "ALS"

            existing_amb = db_session.query(Ambulance).filter_by(vehicle_number=vehicle_number).first()
            if existing_amb:
                existing_amb.driver_id = new_user.id
            else:
                new_amb = Ambulance(
                    vehicle_number=vehicle_number,
                    ambulance_type=ambulance_type,
                    status=AmbulanceStatus.AVAILABLE,
                    driver_id=new_user.id
                )
                db_session.add(new_amb)

        db_session.commit()

        session["user_id"] = new_user.id
        return jsonify({
            "message": f"Registration successful as {target_role.value}.",
            "user": new_user.to_dict()
        }), 201

    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "An unexpected server error occurred."}), 500


@app.route("/api/auth/admin-login", methods=["POST"])
def admin_login():
    """
    Dedicated Master Administration Login Endpoint.
    Only the designated administrator (nileshdr301@gmail.com) is permitted to authenticate.
    """
    try:
        data = request.get_json() or {}
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")

        if not email or not password:
            return jsonify({"error": "Administrator email and password are required."}), 400

        # Strict sole administrator restriction
        if email != "nileshdr301@gmail.com" and email != "admin@rapidaid.org":
            return jsonify({
                "error": "Access Denied: Only the registered Master Administrator (nileshdr301@gmail.com) is permitted to access Administration Login."
            }), 403

        user = db_session.query(User).filter_by(email=email).first()
        if not user or not check_password_hash(user.passwordHash, password):
            return jsonify({"error": "Invalid administrator email or password."}), 401

        if user.role != UserRole.ADMIN:
            return jsonify({"error": "Access Denied: Account lacks Administrator authorization."}), 403

        session["user_id"] = user.id
        return jsonify({
            "message": f"Welcome Administrator, {user.name}. Master system access granted.",
            "user": user.to_dict()
        }), 200

    except Exception as e:
        return jsonify({"error": "An unexpected server error occurred."}), 500


@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        data = request.get_json() or {}
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        portal = data.get("portal", "").strip().upper()

        if not email or not password:
            return jsonify({"error": "Email and password are required."}), 400

        # If portal is specified as ADMIN, enforce master admin
        if portal == "ADMIN" and email != "nileshdr301@gmail.com" and email != "admin@rapidaid.org":
            return jsonify({
                "error": "Access Denied: Only the registered Master Administrator (nileshdr301@gmail.com) can access Administration Login."
            }), 403

        user = db_session.query(User).filter_by(email=email).first()
        if not user or not check_password_hash(user.passwordHash, password):
            return jsonify({"error": "Invalid email or password."}), 401

        if portal == "ADMIN" and user.role != UserRole.ADMIN:
            return jsonify({"error": "Access Denied: Account does not have Administrator privileges."}), 403

        session["user_id"] = user.id
        return jsonify({
            "message": "Login successful.",
            "user": user.to_dict()
        }), 200

    except Exception as e:
        return jsonify({"error": "An unexpected server error occurred."}), 500


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out successfully."}), 200


@app.route("/api/auth/me", methods=["GET"])
def get_current_user():
    user = get_authenticated_user()
    if not user:
        session.clear()
        return jsonify({"authenticated": False, "message": "Not authenticated."}), 401

    return jsonify({
        "authenticated": True,
        "user": user.to_dict()
    }), 200


# --- Emergency Request API Routes (Phase 3 & 4) ---

@app.route("/api/requests", methods=["POST"])
def create_request():
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required to request an ambulance."}), 401

    data = request.get_json() or {}
    emergency_type = data.get("emergency_type", "").strip().lower()
    ambulance_type = data.get("ambulance_type", "ALS").strip().upper()
    pickup_address = data.get("pickup_address", "").strip()
    pickup_lat = data.get("pickup_latitude")
    pickup_lng = data.get("pickup_longitude")
    additional_notes = data.get("additional_notes", "").strip()

    if not emergency_type or emergency_type not in ALLOWED_EMERGENCY_TYPES:
        return jsonify({"error": f"Invalid emergency type. Must be one of: {', '.join(sorted(ALLOWED_EMERGENCY_TYPES))}"}), 400

    if not ambulance_type or ambulance_type not in ALLOWED_AMBULANCE_TYPES:
        return jsonify({"error": f"Invalid ambulance type. Must be one of: {', '.join(sorted(ALLOWED_AMBULANCE_TYPES))}"}), 400

    if not pickup_address:
        return jsonify({"error": "Pickup address is required."}), 400

    if pickup_lat is not None:
        try:
            lat_val = float(pickup_lat)
            if not (-90.0 <= lat_val <= 90.0):
                return jsonify({"error": "Latitude must be between -90 and 90."}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid latitude format."}), 400

    if pickup_lng is not None:
        try:
            lng_val = float(pickup_lng)
            if not (-180.0 <= lng_val <= 180.0):
                return jsonify({"error": "Longitude must be between -180 and 180."}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid longitude format."}), 400

    try:
        new_request = EmergencyRequest(
            user_id=user_id,
            emergency_type=emergency_type,
            ambulance_type=ambulance_type,
            pickup_address=pickup_address,
            pickup_latitude=float(pickup_lat) if pickup_lat is not None else None,
            pickup_longitude=float(pickup_lng) if pickup_lng is not None else None,
            additional_notes=additional_notes if additional_notes else None,
            status=RequestStatus.REQUESTED
        )
        db_session.add(new_request)
        db_session.commit()

        # Automatic Matching Engine Execution (Fastest Arrival Time Optimization)
        match_result = find_and_assign_ambulance(new_request, db_session)

        # Emergency Response Ecosystem Broadcast (Nearby Ambulances & Nearby Hospitals)
        ecosystem_broadcast = broadcast_emergency_alerts(
            new_request,
            match_result.get("ambulance"),
            db_session
        )

        assignment_data = {
            "assigned": match_result["assigned"],
            "distance_km": match_result["distance_km"],
            "eta_minutes": match_result.get("eta_minutes"),
            "mathematical_telemetry": match_result.get("mathematical_telemetry"),
            "message": match_result["message"]
        }
        if match_result["assigned"] and match_result["ambulance"]:
            amb = match_result["ambulance"]
            assignment_data["ambulance"] = {
                "id": amb.id,
                "vehicle_number": amb.vehicle_number,
                "ambulance_type": amb.ambulance_type,
                "status": amb.status.value if isinstance(amb.status, AmbulanceStatus) else str(amb.status)
            }
        else:
            assignment_data["ambulance"] = None

        if match_result["assigned"] and match_result.get("ambulance"):
            msg = f"Fastest unit {match_result['ambulance'].vehicle_number} mathematically assigned (~{match_result.get('eta_minutes', 0)} mins). Alerts dispatched to nearby ambulances and hospitals."
        else:
            msg = "Emergency request created. Searching for available unit in sector."

        return jsonify({
            "message": msg,
            "request": new_request.to_dict(),
            "assignment": assignment_data,
            "ecosystem_broadcast": ecosystem_broadcast
        }), 201

    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "An error occurred while creating emergency request."}), 500


@app.route("/api/requests", methods=["GET"])
def list_user_requests():
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    requests_list = db_session.query(EmergencyRequest)\
        .filter_by(user_id=user_id)\
        .order_by(EmergencyRequest.created_at.desc())\
        .all()

    return jsonify({
        "requests": [req.to_dict() for req in requests_list]
    }), 200


@app.route("/api/requests/<request_id>", methods=["GET"])
def get_request_detail(request_id):
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    req = db_session.get(EmergencyRequest, request_id)
    if not req or req.user_id != user_id:
        return jsonify({"error": "Emergency request not found."}), 404

    return jsonify({
        "request": req.to_dict()
    }), 200


@app.route("/api/requests/<request_id>/cancel", methods=["POST"])
def cancel_request(request_id):
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    req = db_session.get(EmergencyRequest, request_id)
    if not req or req.user_id != user_id:
        return jsonify({"error": "Emergency request not found."}), 404

    if req.status not in (RequestStatus.REQUESTED, RequestStatus.ASSIGNED, RequestStatus.ACCEPTED):
        return jsonify({"error": f"Cannot cancel request in status '{req.status.value}'."}), 400

    try:
        if req.assigned_ambulance:
            req.assigned_ambulance.status = AmbulanceStatus.AVAILABLE

        req.status = RequestStatus.CANCELLED
        db_session.commit()

        return jsonify({
            "message": "Emergency request cancelled successfully.",
            "request": req.to_dict()
        }), 200

    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "An error occurred while cancelling request."}), 500


# --- Driver Workflow API Routes (Phase 5) ---

@app.route("/api/driver/ambulance", methods=["GET"])
@driver_required
def get_driver_ambulance(driver_user):
    amb = db_session.query(Ambulance).filter_by(driver_id=driver_user.id).first()
    if not amb:
        return jsonify({"ambulance": None, "message": "No ambulance linked to this driver account."}), 200

    return jsonify({"ambulance": amb.to_dict()}), 200


@app.route("/api/driver/assignments", methods=["GET"])
@driver_required
def get_driver_assignments(driver_user):
    amb = db_session.query(Ambulance).filter_by(driver_id=driver_user.id).first()
    if not amb:
        return jsonify({"assignments": []}), 200

    assignments = db_session.query(EmergencyRequest)\
        .filter_by(assigned_ambulance_id=amb.id)\
        .order_by(EmergencyRequest.created_at.desc())\
        .all()

    return jsonify({"assignments": [req.to_dict() for req in assignments]}), 200


@app.route("/api/driver/assignments/current", methods=["GET"])
@driver_required
def get_driver_current_assignment(driver_user):
    amb = db_session.query(Ambulance).filter_by(driver_id=driver_user.id).first()
    if not amb:
        return jsonify({"assignment": None}), 200

    active_req = db_session.query(EmergencyRequest)\
        .filter(
            EmergencyRequest.assigned_ambulance_id == amb.id,
            EmergencyRequest.status.in_([RequestStatus.ASSIGNED, RequestStatus.ACCEPTED])
        )\
        .order_by(EmergencyRequest.created_at.desc())\
        .first()

    return jsonify({
        "assignment": active_req.to_dict() if active_req else None
    }), 200


@app.route("/api/driver/assignments/<request_id>/accept", methods=["POST"])
@driver_required
def accept_driver_assignment(driver_user, request_id):
    amb = db_session.query(Ambulance).filter_by(driver_id=driver_user.id).first()
    if not amb:
        return jsonify({"error": "No ambulance linked to this driver."}), 404

    req = db_session.get(EmergencyRequest, request_id)
    if not req or req.assigned_ambulance_id != amb.id:
        return jsonify({"error": "Assignment not found for your ambulance."}), 404

    if req.status == RequestStatus.ACCEPTED:
        return jsonify({"message": "Assignment already accepted.", "request": req.to_dict()}), 200

    if req.status != RequestStatus.ASSIGNED:
        return jsonify({"error": f"Cannot accept request in status '{req.status.value}'."}), 400

    try:
        req.status = RequestStatus.ACCEPTED
        amb.status = AmbulanceStatus.BUSY
        db_session.commit()

        return jsonify({
            "message": "Emergency request accepted.",
            "request": req.to_dict()
        }), 200

    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "Transaction error accepting assignment."}), 500


@app.route("/api/driver/assignments/<request_id>/reject", methods=["POST"])
@driver_required
def reject_driver_assignment(driver_user, request_id):
    amb = db_session.query(Ambulance).filter_by(driver_id=driver_user.id).first()
    if not amb:
        return jsonify({"error": "No ambulance linked to this driver."}), 404

    req = db_session.get(EmergencyRequest, request_id)
    if not req or req.assigned_ambulance_id != amb.id:
        return jsonify({"error": "Assignment not found for your ambulance."}), 404

    if req.status != RequestStatus.ASSIGNED:
        return jsonify({"error": f"Cannot reject request in status '{req.status.value}'."}), 400

    try:
        # Release assignment and make ambulance available for re-matching
        req.assigned_ambulance_id = None
        req.status = RequestStatus.REQUESTED
        amb.status = AmbulanceStatus.AVAILABLE
        db_session.commit()

        return jsonify({
            "message": "Emergency request rejected. Ambulance marked available.",
            "request": req.to_dict()
        }), 200

    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "Transaction error rejecting assignment."}), 500


@app.route("/api/driver/assignments/<request_id>/complete", methods=["POST"])
@driver_required
def complete_driver_assignment(driver_user, request_id):
    amb = db_session.query(Ambulance).filter_by(driver_id=driver_user.id).first()
    if not amb:
        return jsonify({"error": "No ambulance linked to this driver."}), 404

    req = db_session.get(EmergencyRequest, request_id)
    if not req or req.assigned_ambulance_id != amb.id:
        return jsonify({"error": "Assignment not found for your ambulance."}), 404

    if req.status not in (RequestStatus.ACCEPTED, RequestStatus.ASSIGNED):
        return jsonify({"error": f"Cannot complete request in status '{req.status.value}'."}), 400

    try:
        req.status = RequestStatus.COMPLETED
        amb.status = AmbulanceStatus.AVAILABLE
        db_session.commit()

        return jsonify({
            "message": "Emergency trip completed successfully. Ambulance marked available.",
            "request": req.to_dict()
        }), 200

    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "Transaction error completing assignment."}), 500


# --- Driver Telemetry & Location API (Phase 6) ---

@app.route("/api/driver/location", methods=["POST"])
@driver_required
def update_driver_location(driver_user):
    amb = db_session.query(Ambulance).filter_by(driver_id=driver_user.id).first()
    if not amb:
        return jsonify({"error": "No ambulance vehicle linked to this driver account."}), 404

    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid or missing JSON payload."}), 400

    lat = data.get("latitude")
    lng = data.get("longitude")
    speed = data.get("speed")
    heading = data.get("heading")

    if lat is None or lng is None:
        return jsonify({"error": "Both latitude and longitude are required."}), 400

    try:
        lat_val = float(lat)
        lng_val = float(lng)
    except (ValueError, TypeError):
        return jsonify({"error": "Latitude and longitude must be valid numeric values."}), 400

    if not (-90.0 <= lat_val <= 90.0):
        return jsonify({"error": "Latitude must be between -90 and 90."}), 400

    if not (-180.0 <= lng_val <= 180.0):
        return jsonify({"error": "Longitude must be between -180 and 180."}), 400

    speed_val = None
    if speed is not None:
        try:
            speed_val = float(speed)
            if speed_val < 0:
                return jsonify({"error": "Speed cannot be negative."}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Speed must be a valid numeric value."}), 400

    heading_val = None
    if heading is not None:
        try:
            heading_val = float(heading)
        except (ValueError, TypeError):
            return jsonify({"error": "Heading must be a valid numeric value."}), 400

    try:
        amb.latitude = lat_val
        amb.longitude = lng_val
        amb.speed = speed_val
        amb.heading = heading_val
        amb.location_updated_at = datetime.now(timezone.utc)
        db_session.commit()

        return jsonify({
            "message": "Ambulance location updated successfully.",
            "ambulance": amb.to_dict()
        }), 200

    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "Transaction error updating location."}), 500


# --- Patient Real-Time Tracking & ETA API (Phase 6) ---

@app.route("/api/requests/<request_id>/tracking", methods=["GET"])
def get_request_tracking(request_id):
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    req = db_session.get(EmergencyRequest, request_id)
    if not req:
        return jsonify({"error": "Emergency request not found."}), 404

    # Authorization check: request owner, assigned driver, hospital or admin
    user = db_session.get(User, user_id)
    is_owner = (req.user_id == user_id)
    is_driver = False
    is_privileged = False
    if user:
        if user.role in (UserRole.ADMIN, UserRole.HOSPITAL):
            is_privileged = True
        elif user.role == UserRole.DRIVER:
            driver_amb = db_session.query(Ambulance).filter_by(driver_id=user_id).first()
            if driver_amb and req.assigned_ambulance_id == driver_amb.id:
                is_driver = True

    if not (is_owner or is_driver or is_privileged):
        return jsonify({"error": "Access denied. You are not authorized to track this request."}), 403

    amb_data = None
    eta_data = {
        "available": False,
        "minutes": None,
        "distance_km": None,
        "formatted": "ETA unavailable",
        "message": "Searching for an ambulance...",
        "is_stale": False,
        "stale_message": None
    }

    driver_data = None
    if req.assigned_ambulance:
        amb = req.assigned_ambulance
        amb_data = amb.to_dict()
        drv = amb.driver or (db_session.get(User, amb.driver_id) if amb.driver_id else None)
        if drv:
            driver_data = {
                "id": drv.id,
                "name": drv.name,
                "phone": drv.phone,
                "email": drv.email
            }
        else:
            driver_data = {
                "id": None,
                "name": "On-Duty Paramedic",
                "phone": "+1 (555) 987-6543",
                "email": "driver@rapidaid.org"
            }

        if req.status in (RequestStatus.ASSIGNED, RequestStatus.ACCEPTED):
            eta_data = calculate_eta(
                amb.latitude,
                amb.longitude,
                req.pickup_latitude,
                req.pickup_longitude,
                speed_kmh=amb.speed,
                location_updated_at=amb.location_updated_at
            )

    is_driver_accepted = (req.status == RequestStatus.ACCEPTED)
    acceptance_msg = None
    if is_driver_accepted and amb_data:
        d_name = driver_data["name"] if driver_data else "The paramedic"
        d_phone = driver_data["phone"] if driver_data else "Emergency Dispatch"
        eta_min_str = f"~{eta_data['minutes']} mins" if eta_data.get('minutes') else "En Route"
        acceptance_msg = (
            f"✅ Driver {d_name} (Phone: {d_phone}) has ACCEPTED your emergency call! "
            f"Ambulance {amb_data['vehicle_number']} ({amb_data['ambulance_type']}) is en route to {req.pickup_address}. "
            f"Estimated arrival time: {eta_min_str}."
        )

    return jsonify({
        "request_id": req.id,
        "status": req.status.value if isinstance(req.status, RequestStatus) else str(req.status),
        "emergency_type": req.emergency_type,
        "ambulance_type": req.ambulance_type,
        "pickup_address": req.pickup_address,
        "pickup_latitude": req.pickup_latitude,
        "pickup_longitude": req.pickup_longitude,
        "ambulance": amb_data,
        "driver": driver_data,
        "driver_accepted": is_driver_accepted,
        "acceptance_message": acceptance_msg,
        "eta": eta_data,
        "is_stale": eta_data.get("is_stale", False),
        "last_updated_seconds_ago": eta_data.get("last_updated_seconds_ago"),
        "created_at": req.created_at.isoformat() if req.created_at else None
    }), 200


@app.route("/api/requests/<request_id>/stream", methods=["GET"])
def stream_request_tracking(request_id):
    """Server-Sent Events (SSE) endpoint for live location updates."""
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    req = db_session.get(EmergencyRequest, request_id)
    if not req:
        return jsonify({"error": "Emergency request not found."}), 404

    # Authorization check
    user = db_session.get(User, user_id)
    is_owner = (req.user_id == user_id)
    is_driver = False
    if user and (user.role == UserRole.DRIVER or user.role == UserRole.ADMIN):
        driver_amb = db_session.query(Ambulance).filter_by(driver_id=user_id).first()
        if driver_amb and req.assigned_ambulance_id == driver_amb.id:
            is_driver = True
        elif user.role == UserRole.ADMIN:
            is_driver = True

    if not (is_owner or is_driver):
        return jsonify({"error": "Access denied. You are not authorized to stream tracking for this request."}), 403

    import json

    def event_stream():
        with app.app_context():
            req_curr = db_session.get(EmergencyRequest, request_id)
            if not req_curr:
                return
            
            amb_curr = req_curr.assigned_ambulance
            eta_info = calculate_eta(
                amb_curr.latitude if amb_curr else None,
                amb_curr.longitude if amb_curr else None,
                req_curr.pickup_latitude,
                req_curr.pickup_longitude,
                speed_kmh=amb_curr.speed if amb_curr else None,
                location_updated_at=amb_curr.location_updated_at if amb_curr else None
            ) if amb_curr else {
                "available": False,
                "minutes": None,
                "distance_km": None,
                "formatted": "ETA unavailable",
                "message": "Searching for an ambulance...",
                "is_stale": False
            }

            driver_info = None
            if amb_curr:
                drv = amb_curr.driver or (db_session.get(User, amb_curr.driver_id) if amb_curr.driver_id else None)
                if drv:
                    driver_info = {"name": drv.name, "phone": drv.phone, "email": drv.email}

            payload = {
                "request_id": req_curr.id,
                "status": req_curr.status.value if isinstance(req_curr.status, RequestStatus) else str(req_curr.status),
                "emergency_type": req_curr.emergency_type,
                "ambulance_type": req_curr.ambulance_type,
                "pickup_address": req_curr.pickup_address,
                "pickup_latitude": req_curr.pickup_latitude,
                "pickup_longitude": req_curr.pickup_longitude,
                "ambulance": amb_curr.to_dict() if amb_curr else None,
                "driver": driver_info,
                "driver_accepted": (req_curr.status == RequestStatus.ACCEPTED),
                "eta": eta_info,
                "is_stale": eta_info.get("is_stale", False),
                "last_updated_seconds_ago": eta_info.get("last_updated_seconds_ago")
            }
            yield f"data: {json.dumps(payload)}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


# --- Real-Time Notification & SSE Infrastructure (Phase 2) ---

@app.route("/api/notifications/stream", methods=["GET"])
def stream_ecosystem_notifications():
    """
    Authenticated Server-Sent Events stream delivering real-time alerts.
    Enforces role-based isolation:
      - Drivers only receive driver-targeted events (for their unit/user id).
      - Hospitals only receive hospital alerts.
      - Citizens only receive updates for their own emergency requests.
      - Administrators receive fleet-wide events.
    Supports:
      - Last-Event-ID header or last_event_id query param for reconnection / catching up.
      - Heartbeat ping comments every 15s to keep connections alive through proxies.
      - Safe queue termination on client disconnect.
      - Test mode support (yielding initial events and exiting to avoid blocking test runs).
    """
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    user = db_session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found."}), 401

    channels = [f"user:{user_id}"]
    if user.role == UserRole.DRIVER:
        channels.append("role:DRIVER")
        driver_amb = db_session.query(Ambulance).filter_by(driver_id=user_id).first()
        if driver_amb:
            channels.append(f"ambulance:{driver_amb.id}")
    elif user.role == UserRole.HOSPITAL:
        channels.append("role:HOSPITAL")
        hosp = db_session.query(Hospital).filter_by(user_id=user_id).first()
        if hosp:
            channels.append(f"hospital:{hosp.id}")
    elif user.role == UserRole.ADMIN:
        channels.extend(["role:ADMIN", "role:DRIVER", "role:HOSPITAL"])
    elif user.role == UserRole.USER:
        channels.append("role:USER")

    last_event_id = request.headers.get("Last-Event-ID") or request.args.get("last_event_id")
    is_persistent = bool(request.args.get("persistent"))
    is_testing = bool(app.config.get("TESTING", False))

    def event_stream():
        q = event_bus.subscribe(channels)
        try:
            # 1. Catch up on unacknowledged / pending notifications for this recipient
            with app.app_context():
                query = db_session.query(EcosystemNotification).filter(
                    EcosystemNotification.delivery_status != "ACKNOWLEDGED"
                )
                if user.role == UserRole.DRIVER:
                    driver_amb = db_session.query(Ambulance).filter_by(driver_id=user_id).first()
                    amb_id = driver_amb.id if driver_amb else ""
                    query = query.filter(
                        (EcosystemNotification.recipient_user_id == user_id) |
                        (EcosystemNotification.recipient_ambulance_id == amb_id) |
                        (EcosystemNotification.recipient_role == "DRIVER")
                    )
                elif user.role == UserRole.HOSPITAL:
                    hosp = db_session.query(Hospital).filter_by(user_id=user_id).first()
                    hosp_id = hosp.id if hosp else ""
                    query = query.filter(
                        (EcosystemNotification.recipient_user_id == user_id) |
                        (EcosystemNotification.recipient_hospital_id == hosp_id) |
                        (EcosystemNotification.recipient_role == "HOSPITAL")
                    )
                elif user.role == UserRole.ADMIN:
                    pass
                else:
                    query = query.filter(EcosystemNotification.recipient_user_id == user_id)

                pending_events = query.order_by(EcosystemNotification.created_at.desc()).limit(15).all()
                for notif in reversed(pending_events):
                    notif_data = notif.to_dict()
                    yield f"id: {notif.id}\nevent: {notif.event_type}\ndata: {json.dumps(notif_data)}\n\n"

            # 2. Connection established heartbeat
            yield f": connected {datetime.now(timezone.utc).isoformat()}\n\n"

            # In unit test mode without persistent flag, stop here to avoid blocking tests
            if is_testing and not is_persistent:
                return

            last_heartbeat = time.time()
            while True:
                try:
                    event_data = q.get(timeout=2.0)
                    evt_id = event_data.get("id", str(time.time()))
                    evt_type = event_data.get("event_type", "message")
                    yield f"id: {evt_id}\nevent: {evt_type}\ndata: {json.dumps(event_data)}\n\n"
                    last_heartbeat = time.time()
                except queue.Empty:
                    if time.time() - last_heartbeat > 15:
                        yield f": keepalive {datetime.now(timezone.utc).isoformat()}\n\n"
                        last_heartbeat = time.time()
        finally:
            event_bus.unsubscribe(q, channels)

    response = Response(event_stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/api/notifications/<notification_id>/ack", methods=["POST"])
def acknowledge_notification(notification_id):
    """Acknowledge receipt and reading of an ecosystem notification."""
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    user = db_session.get(User, user_id)
    notif = db_session.get(EcosystemNotification, notification_id)
    if not notif:
        return jsonify({"error": "Notification not found."}), 404

    is_authorized = (
        user.role == UserRole.ADMIN or
        notif.recipient_user_id == user_id or
        (user.role == UserRole.DRIVER and notif.recipient_role == "DRIVER") or
        (user.role == UserRole.HOSPITAL and notif.recipient_role == "HOSPITAL")
    )
    if not is_authorized:
        return jsonify({"error": "Access denied. Cannot acknowledge this notification."}), 403

    try:
        notif.delivery_status = "ACKNOWLEDGED"
        notif.acknowledged_at = datetime.now(timezone.utc)
        db_session.commit()
        return jsonify({
            "message": "Notification acknowledged.",
            "notification": notif.to_dict()
        }), 200
    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "Failed to acknowledge notification."}), 500


@app.route("/api/notifications", methods=["GET"])
def get_user_notifications():
    """Retrieve recent notifications for the authenticated user's role and identity."""
    user_id = get_authenticated_user_id()
    if not user_id:
        return jsonify({"error": "Authentication required."}), 401

    user = db_session.get(User, user_id)
    query = db_session.query(EcosystemNotification)
    if user.role == UserRole.DRIVER:
        driver_amb = db_session.query(Ambulance).filter_by(driver_id=user_id).first()
        amb_id = driver_amb.id if driver_amb else ""
        query = query.filter(
            (EcosystemNotification.recipient_user_id == user_id) |
            (EcosystemNotification.recipient_ambulance_id == amb_id) |
            (EcosystemNotification.recipient_role == "DRIVER")
        )
    elif user.role == UserRole.HOSPITAL:
        hosp = db_session.query(Hospital).filter_by(user_id=user_id).first()
        hosp_id = hosp.id if hosp else ""
        query = query.filter(
            (EcosystemNotification.recipient_user_id == user_id) |
            (EcosystemNotification.recipient_hospital_id == hosp_id) |
            (EcosystemNotification.recipient_role == "HOSPITAL")
        )
    elif user.role == UserRole.ADMIN:
        pass
    else:
        query = query.filter(EcosystemNotification.recipient_user_id == user_id)

    notifs = query.order_by(EcosystemNotification.created_at.desc()).limit(50).all()
    return jsonify({"notifications": [n.to_dict() for n in notifs]}), 200


# --- Admin Dashboard & Dispatch Control APIs (Phase 7) ---

@app.route("/api/admin/overview", methods=["GET"])
@admin_required
def get_admin_overview(admin_user):
    total_ambs = db_session.query(Ambulance).count()
    available_ambs = db_session.query(Ambulance).filter_by(status=AmbulanceStatus.AVAILABLE).count()
    busy_ambs = db_session.query(Ambulance).filter_by(status=AmbulanceStatus.BUSY).count()
    offline_ambs = db_session.query(Ambulance).filter_by(status=AmbulanceStatus.OFFLINE).count()

    total_reqs = db_session.query(EmergencyRequest).count()
    requested_reqs = db_session.query(EmergencyRequest).filter_by(status=RequestStatus.REQUESTED).count()
    assigned_reqs = db_session.query(EmergencyRequest).filter_by(status=RequestStatus.ASSIGNED).count()
    accepted_reqs = db_session.query(EmergencyRequest).filter_by(status=RequestStatus.ACCEPTED).count()
    completed_reqs = db_session.query(EmergencyRequest).filter_by(status=RequestStatus.COMPLETED).count()
    cancelled_reqs = db_session.query(EmergencyRequest).filter_by(status=RequestStatus.CANCELLED).count()

    total_drivers = db_session.query(User).filter_by(role=UserRole.DRIVER).count()
    total_users = db_session.query(User).filter_by(role=UserRole.USER).count()

    return jsonify({
        "ambulances": {
            "total": total_ambs,
            "available": available_ambs,
            "busy": busy_ambs,
            "offline": offline_ambs
        },
        "requests": {
            "total": total_reqs,
            "requested": requested_reqs,
            "assigned": assigned_reqs,
            "accepted": accepted_reqs,
            "active": assigned_reqs + accepted_reqs,
            "completed": completed_reqs,
            "cancelled": cancelled_reqs
        },
        "drivers": {
            "total": total_drivers
        },
        "users": {
            "total": total_users
        }
    }), 200


@app.route("/api/admin/requests", methods=["GET"])
@admin_required
def get_admin_requests(admin_user):
    query = db_session.query(EmergencyRequest)

    status_filter = request.args.get("status")
    emergency_type_filter = request.args.get("emergency_type")
    ambulance_type_filter = request.args.get("ambulance_type")

    if status_filter:
        try:
            req_status = RequestStatus(status_filter.upper())
            query = query.filter(EmergencyRequest.status == req_status)
        except ValueError:
            pass

    if emergency_type_filter:
        query = query.filter(EmergencyRequest.emergency_type == emergency_type_filter.lower())

    if ambulance_type_filter:
        query = query.filter(EmergencyRequest.ambulance_type == ambulance_type_filter.upper())

    requests_list = query.order_by(EmergencyRequest.created_at.desc()).all()

    output = []
    for req in requests_list:
        req_dict = req.to_dict()

        if req.user:
            req_dict["patient"] = {
                "id": req.user.id,
                "name": req.user.name,
                "email": req.user.email,
                "phone": req.user.phone
            }
        else:
            req_dict["patient"] = None

        if req.assigned_ambulance and req.status in (RequestStatus.ASSIGNED, RequestStatus.ACCEPTED):
            amb = req.assigned_ambulance
            eta_info = calculate_eta(
                amb.latitude,
                amb.longitude,
                req.pickup_latitude,
                req.pickup_longitude,
                speed_kmh=amb.speed,
                location_updated_at=amb.location_updated_at
            )
            req_dict["eta"] = eta_info
        else:
            req_dict["eta"] = None

        output.append(req_dict)

    return jsonify({"requests": output}), 200


@app.route("/api/admin/requests/<request_id>", methods=["GET"])
@admin_required
def get_admin_request_detail(admin_user, request_id):
    req = db_session.get(EmergencyRequest, request_id)
    if not req:
        return jsonify({"error": "Emergency request not found."}), 404

    req_dict = req.to_dict()
    if req.user:
        req_dict["patient"] = {
            "id": req.user.id,
            "name": req.user.name,
            "email": req.user.email,
            "phone": req.user.phone
        }

    if req.assigned_ambulance:
        amb = req.assigned_ambulance
        driver = db_session.query(User).get(amb.driver_id) if amb.driver_id else None
        req_dict["driver"] = {
            "id": driver.id,
            "name": driver.name,
            "email": driver.email,
            "phone": driver.phone
        } if driver else None

        eta_info = calculate_eta(
            amb.latitude,
            amb.longitude,
            req.pickup_latitude,
            req.pickup_longitude,
            speed_kmh=amb.speed,
            location_updated_at=amb.location_updated_at
        )
        req_dict["eta"] = eta_info

    return jsonify({"request": req_dict}), 200


@app.route("/api/admin/active-trips", methods=["GET"])
@admin_required
def get_admin_active_trips(admin_user):
    active_requests = db_session.query(EmergencyRequest).filter(
        EmergencyRequest.status.in_([RequestStatus.ASSIGNED, RequestStatus.ACCEPTED])
    ).order_by(EmergencyRequest.created_at.desc()).all()

    trips = []
    for req in active_requests:
        req_dict = req.to_dict()
        if req.user:
            req_dict["patient"] = {
                "name": req.user.name,
                "phone": req.user.phone
            }
        
        amb = req.assigned_ambulance
        if amb:
            driver = amb.driver or (db_session.get(User, amb.driver_id) if amb.driver_id else None)
            req_dict["driver"] = {
                "name": driver.name if driver else "Unassigned",
                "phone": driver.phone if driver else "-"
            }
            eta_info = calculate_eta(
                amb.latitude,
                amb.longitude,
                req.pickup_latitude,
                req.pickup_longitude,
                speed_kmh=amb.speed,
                location_updated_at=amb.location_updated_at
            )
            req_dict["eta"] = eta_info

        trips.append(req_dict)

    return jsonify({"active_trips": trips}), 200


@app.route("/api/admin/ambulances", methods=["GET"])
@admin_required
def get_admin_ambulances(admin_user):
    query = db_session.query(Ambulance)

    status_filter = request.args.get("status")
    type_filter = request.args.get("ambulance_type")

    if status_filter:
        try:
            amb_status = AmbulanceStatus(status_filter.upper())
            query = query.filter(Ambulance.status == amb_status)
        except ValueError:
            pass

    if type_filter:
        query = query.filter(Ambulance.ambulance_type == type_filter.upper())

    ambulances = query.order_by(Ambulance.vehicle_number.asc()).all()

    output = []
    for amb in ambulances:
        amb_dict = amb.to_dict()
        driver = db_session.get(User, amb.driver_id) if amb.driver_id else None
        amb_dict["driver"] = {
            "id": driver.id,
            "name": driver.name,
            "email": driver.email,
            "phone": driver.phone
        } if driver else None

        active_req = db_session.query(EmergencyRequest).filter(
            EmergencyRequest.assigned_ambulance_id == amb.id,
            EmergencyRequest.status.in_([RequestStatus.ASSIGNED, RequestStatus.ACCEPTED])
        ).order_by(EmergencyRequest.created_at.desc()).first()
        amb_dict["current_assignment"] = {
            "id": active_req.id,
            "status": active_req.status.value,
            "pickup_address": active_req.pickup_address
        } if active_req else None

        output.append(amb_dict)

    return jsonify({"ambulances": output}), 200


@app.route("/api/admin/ambulances/<ambulance_id>", methods=["GET"])
@admin_required
def get_admin_ambulance_detail(admin_user, ambulance_id):
    amb = db_session.get(Ambulance, ambulance_id)
    if not amb:
        return jsonify({"error": "Ambulance not found."}), 404

    amb_dict = amb.to_dict()
    driver = db_session.get(User, amb.driver_id) if amb.driver_id else None
    amb_dict["driver"] = driver.to_dict() if driver else None

    active_req = db_session.query(EmergencyRequest).filter(
        EmergencyRequest.assigned_ambulance_id == amb.id,
        EmergencyRequest.status.in_([RequestStatus.ASSIGNED, RequestStatus.ACCEPTED])
    ).first()
    amb_dict["current_assignment"] = active_req.to_dict() if active_req else None

    return jsonify({"ambulance": amb_dict}), 200


@app.route("/api/admin/ambulances/<ambulance_id>/status", methods=["POST", "PUT"])
@admin_required
def update_admin_ambulance_status(admin_user, ambulance_id):
    amb = db_session.get(Ambulance, ambulance_id)
    if not amb:
        return jsonify({"error": "Ambulance not found."}), 404

    data = request.get_json() or {}
    new_status_str = data.get("status", "").strip().upper()

    try:
        target_status = AmbulanceStatus(new_status_str)
    except ValueError:
        return jsonify({"error": "Invalid status value. Must be AVAILABLE, BUSY, or OFFLINE."}), 400

    # Safety check: Do NOT allow changing to OFFLINE if unit is assigned to an active request
    if target_status == AmbulanceStatus.OFFLINE:
        active_req = db_session.query(EmergencyRequest).filter(
            EmergencyRequest.assigned_ambulance_id == amb.id,
            EmergencyRequest.status.in_([RequestStatus.ASSIGNED, RequestStatus.ACCEPTED])
        ).first()
        if active_req:
            return jsonify({"error": f"Cannot mark ambulance OFFLINE while assigned to an active emergency request (#{active_req.id[:8]})."}), 400

    try:
        amb.status = target_status
        db_session.commit()
        return jsonify({
            "message": f"Ambulance {amb.vehicle_number} status updated to '{target_status.value}'.",
            "ambulance": amb.to_dict()
        }), 200
    except Exception as e:
        db_session.rollback()
        return jsonify({"error": "Transaction error updating ambulance status."}), 500


@app.route("/api/admin/drivers", methods=["GET"])
@admin_required
def get_admin_drivers(admin_user):
    drivers = db_session.query(User).filter_by(role=UserRole.DRIVER).all()

    output = []
    for d in drivers:
        d_dict = d.to_dict()
        amb = db_session.query(Ambulance).filter_by(driver_id=d.id).first()
        d_dict["assigned_ambulance"] = amb.to_dict() if amb else None

        active_req = None
        if amb:
            active_req = db_session.query(EmergencyRequest).filter(
                EmergencyRequest.assigned_ambulance_id == amb.id,
                EmergencyRequest.status.in_([RequestStatus.ASSIGNED, RequestStatus.ACCEPTED])
            ).first()
        d_dict["current_assignment"] = active_req.to_dict() if active_req else None

        output.append(d_dict)

    return jsonify({"drivers": output}), 200


@app.route("/api/admin/drivers/<driver_id>", methods=["GET"])
@admin_required
def get_admin_driver_detail(admin_user, driver_id):
    driver = db_session.get(User, driver_id)
    if not driver or driver.role != UserRole.DRIVER:
        return jsonify({"error": "Driver user not found."}), 404

    d_dict = driver.to_dict()
    amb = db_session.query(Ambulance).filter_by(driver_id=driver.id).first()
    d_dict["assigned_ambulance"] = amb.to_dict() if amb else None

    recent_trips = []
    if amb:
        trips = db_session.query(EmergencyRequest).filter_by(assigned_ambulance_id=amb.id)\
            .order_by(EmergencyRequest.created_at.desc()).limit(10).all()
        recent_trips = [t.to_dict() for t in trips]
    d_dict["recent_trips"] = recent_trips

    return jsonify({"driver": d_dict}), 200


@app.route("/api/admin/users", methods=["GET"])
@admin_required
def get_admin_users(admin_user):
    query = db_session.query(User)

    role_filter = request.args.get("role")
    if role_filter:
        try:
            target_role = UserRole(role_filter.upper())
            query = query.filter(User.role == target_role)
        except ValueError:
            pass

    users = query.order_by(User.createdAt.desc()).all()

    output = []
    for u in users:
        u_dict = u.to_dict()
        req_count = db_session.query(EmergencyRequest).filter_by(user_id=u.id).count()
        u_dict["total_requests"] = req_count
        output.append(u_dict)

    return jsonify({"users": output}), 200


@app.route("/api/admin/users/<user_id>", methods=["GET"])
@admin_required
def get_admin_user_detail(admin_user, user_id):
    user = db_session.get(User, user_id)
    if not user:
        return jsonify({"error": "User record not found."}), 404

    u_dict = user.to_dict()
    requests_list = db_session.query(EmergencyRequest).filter_by(user_id=user.id)\
        .order_by(EmergencyRequest.created_at.desc()).all()
    u_dict["requests"] = [r.to_dict() for r in requests_list]

    return jsonify({"user": u_dict}), 200


# --- Admin Master Database Explorer APIs ---

@app.route("/api/admin/database/overview", methods=["GET"])
@admin_required
def get_admin_database_overview(admin_user):
    """
    Returns full visibility into all database entities and tables for the Master Administrator.
    """
    try:
        users = db_session.query(User).order_by(User.createdAt.desc()).all()
        ambulances = db_session.query(Ambulance).order_by(Ambulance.created_at.desc()).all()
        requests_list = db_session.query(EmergencyRequest).order_by(EmergencyRequest.created_at.desc()).all()
        hospitals = db_session.query(Hospital).order_by(Hospital.created_at.desc()).all()
        alerts = db_session.query(EmergencyAlert).order_by(EmergencyAlert.created_at.desc()).limit(150).all()

        users_payload = []
        for u in users:
            ud = u.to_dict()
            ud["role"] = u.role.value if hasattr(u.role, "value") else str(u.role)
            ud["total_requests"] = len(u.requests)
            assigned_amb = db_session.query(Ambulance).filter_by(driver_id=u.id).first()
            ud["assigned_vehicle"] = assigned_amb.vehicle_number if assigned_amb else None
            users_payload.append(ud)

        ambulances_payload = []
        for a in ambulances:
            ad = a.to_dict()
            driver = db_session.get(User, a.driver_id) if a.driver_id else None
            ad["driver_name"] = driver.name if driver else "Unassigned"
            ad["driver_phone"] = driver.phone if driver else "-"
            active_req = db_session.query(EmergencyRequest).filter(
                EmergencyRequest.assigned_ambulance_id == a.id,
                EmergencyRequest.status.in_([RequestStatus.ASSIGNED, RequestStatus.ACCEPTED])
            ).first()
            ad["active_request_id"] = active_req.id if active_req else None
            ambulances_payload.append(ad)

        requests_payload = []
        for r in requests_list:
            rd = r.to_dict()
            rd["patient_name"] = r.user.name if r.user else "Unknown"
            rd["patient_phone"] = r.user.phone if r.user else "-"
            rd["patient_email"] = r.user.email if r.user else "-"
            rd["ambulance_number"] = r.assigned_ambulance.vehicle_number if r.assigned_ambulance else "Unassigned"
            requests_payload.append(rd)

        hospitals_payload = [h.to_dict() for h in hospitals]
        alerts_payload = [al.to_dict() for al in alerts]

        return jsonify({
            "database": {
                "engine": "SQLAlchemy (SQLite / PostgreSQL)",
                "file": "rapidaid.db",
                "master_admin": admin_user.email
            },
            "counts": {
                "users": len(users_payload),
                "ambulances": len(ambulances_payload),
                "emergency_requests": len(requests_payload),
                "hospitals": len(hospitals_payload),
                "emergency_alerts": len(alerts_payload)
            },
            "tables": {
                "users": users_payload,
                "ambulances": ambulances_payload,
                "emergency_requests": requests_payload,
                "hospitals": hospitals_payload,
                "emergency_alerts": alerts_payload
            }
        }), 200

    except Exception as e:
        app.logger.error(f"Error fetching database overview: {str(e)}")
        return jsonify({"error": "Failed to load database overview."}), 500


@app.route("/api/admin/database/table/<table_name>", methods=["GET"])
@admin_required
def get_admin_database_table(admin_user, table_name):
    """
    Returns records for a specific table with optional search query.
    """
    search_q = (request.args.get("q") or "").strip().lower()
    table_name = table_name.lower()

    if table_name == "users":
        query = db_session.query(User)
        if search_q:
            query = query.filter(
                (User.name.ilike(f"%{search_q}%")) |
                (User.email.ilike(f"%{search_q}%")) |
                (User.phone.ilike(f"%{search_q}%"))
            )
        rows = [u.to_dict() for u in query.order_by(User.createdAt.desc()).all()]
    elif table_name in ("ambulances", "ambulance"):
        query = db_session.query(Ambulance)
        if search_q:
            query = query.filter(
                (Ambulance.vehicle_number.ilike(f"%{search_q}%")) |
                (Ambulance.ambulance_type.ilike(f"%{search_q}%"))
            )
        rows = [a.to_dict() for a in query.order_by(Ambulance.created_at.desc()).all()]
    elif table_name in ("requests", "emergency_requests"):
        query = db_session.query(EmergencyRequest)
        if search_q:
            query = query.filter(
                (EmergencyRequest.pickup_address.ilike(f"%{search_q}%")) |
                (EmergencyRequest.emergency_type.ilike(f"%{search_q}%"))
            )
        rows = [r.to_dict() for r in query.order_by(EmergencyRequest.created_at.desc()).all()]
    elif table_name in ("hospitals", "hospital"):
        query = db_session.query(Hospital)
        if search_q:
            query = query.filter(
                (Hospital.name.ilike(f"%{search_q}%")) |
                (Hospital.address.ilike(f"%{search_q}%")) |
                (Hospital.phone.ilike(f"%{search_q}%")) |
                (Hospital.city.ilike(f"%{search_q}%")) |
                (Hospital.taluka.ilike(f"%{search_q}%")) |
                (Hospital.district.ilike(f"%{search_q}%")) |
                (Hospital.pincode.ilike(f"%{search_q}%"))
            )
        rows = [h.to_dict() for h in query.order_by(Hospital.name.asc()).all()]
    elif table_name in ("alerts", "emergency_alerts"):
        query = db_session.query(EmergencyAlert)
        if search_q:
            query = query.filter(
                (EmergencyAlert.recipient_name.ilike(f"%{search_q}%")) |
                (EmergencyAlert.message.ilike(f"%{search_q}%"))
            )
        rows = [al.to_dict() for al in query.order_by(EmergencyAlert.created_at.desc()).all()]
    else:
        return jsonify({"error": f"Table '{table_name}' not recognized."}), 404

    return jsonify({
        "table": table_name,
        "count": len(rows),
        "rows": rows
    }), 200


# --- Hospital Portal API Routes ---

@app.route("/api/hospital/overview", methods=["GET"])
@hospital_required
def get_hospital_overview(hospital_user):
    active_requests = db_session.query(EmergencyRequest).filter(
        EmergencyRequest.status.in_([RequestStatus.REQUESTED, RequestStatus.ASSIGNED, RequestStatus.ACCEPTED])
    ).order_by(EmergencyRequest.created_at.desc()).all()

    available_ambs = db_session.query(Ambulance).filter_by(status=AmbulanceStatus.AVAILABLE).count()
    busy_ambs = db_session.query(Ambulance).filter_by(status=AmbulanceStatus.BUSY).count()
    total_ambs = db_session.query(Ambulance).count()

    reqs_output = []
    for req in active_requests:
        rd = req.to_dict()
        if req.user:
            rd["patient"] = {
                "name": req.user.name,
                "phone": req.user.phone,
                "email": req.user.email
            }
        if req.assigned_ambulance:
            amb = req.assigned_ambulance
            driver = db_session.get(User, amb.driver_id) if amb.driver_id else None
            rd["driver"] = {"name": driver.name, "phone": driver.phone} if driver else None
            eta_info = calculate_eta(
                amb.latitude, amb.longitude,
                req.pickup_latitude, req.pickup_longitude,
                speed_kmh=amb.speed, location_updated_at=amb.location_updated_at
            )
            rd["eta"] = eta_info
        reqs_output.append(rd)

    hosp_facility = db_session.query(Hospital).filter_by(user_id=hospital_user.id).first()
    incoming_alerts_query = db_session.query(EmergencyAlert).filter(
        EmergencyAlert.recipient_type == "HOSPITAL"
    )
    if hosp_facility:
        incoming_alerts_query = incoming_alerts_query.filter(EmergencyAlert.recipient_id == hosp_facility.id)
    incoming_alerts = incoming_alerts_query.order_by(EmergencyAlert.created_at.desc()).limit(15).all()

    return jsonify({
        "hospital": hospital_user.to_dict(),
        "facility": hosp_facility.to_dict() if hosp_facility else None,
        "stats": {
            "active_emergencies": len(active_requests),
            "available_ambulances": available_ambs,
            "busy_ambulances": busy_ambs,
            "total_ambulances": total_ambs
        },
        "incoming_alerts": [a.to_dict() for a in incoming_alerts],
        "active_requests": reqs_output
    }), 200


# --- Emergency Ecosystem Alerts & Hospital Directory APIs ---

@app.route("/api/alerts", methods=["GET"])
def get_user_alerts():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Authentication required."}), 401

    limit = min(50, int(request.args.get("limit", 20)))

    if user.role == UserRole.DRIVER:
        amb = db_session.query(Ambulance).filter_by(driver_id=user.id).first()
        if amb:
            alerts = db_session.query(EmergencyAlert).filter(
                EmergencyAlert.recipient_type == "AMBULANCE",
                EmergencyAlert.recipient_id == amb.id
            ).order_by(EmergencyAlert.created_at.desc()).limit(limit).all()
        else:
            alerts = []
    elif user.role == UserRole.HOSPITAL:
        hosp = db_session.query(Hospital).filter_by(user_id=user.id).first()
        if hosp:
            alerts = db_session.query(EmergencyAlert).filter(
                EmergencyAlert.recipient_type == "HOSPITAL",
                EmergencyAlert.recipient_id == hosp.id
            ).order_by(EmergencyAlert.created_at.desc()).limit(limit).all()
        else:
            alerts = db_session.query(EmergencyAlert).filter(
                EmergencyAlert.recipient_type == "HOSPITAL"
            ).order_by(EmergencyAlert.created_at.desc()).limit(limit).all()
    elif user.role == UserRole.ADMIN:
        alerts = db_session.query(EmergencyAlert).order_by(EmergencyAlert.created_at.desc()).limit(limit).all()
    else:
        req_ids = [r.id for r in user.requests]
        if req_ids:
            alerts = db_session.query(EmergencyAlert).filter(
                EmergencyAlert.request_id.in_(req_ids)
            ).order_by(EmergencyAlert.created_at.desc()).limit(limit).all()
        else:
            alerts = []

    return jsonify({
        "alerts": [a.to_dict() for a in alerts]
    }), 200


@app.route("/api/alerts/<alert_id>/read", methods=["POST"])
def mark_alert_read(alert_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Authentication required."}), 401

    alert = db_session.get(EmergencyAlert, alert_id)
    if not alert:
        return jsonify({"error": "Alert not found."}), 404

    alert.is_read = datetime.now(timezone.utc)
    try:
        db_session.commit()
        return jsonify({"message": "Alert marked as read.", "alert": alert.to_dict()}), 200
    except Exception:
        db_session.rollback()
        return jsonify({"error": "Failed to update alert status."}), 500


@app.route("/api/requests/<request_id>/alerts", methods=["GET"])
def get_request_alerts(request_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Authentication required."}), 401

    req = db_session.get(EmergencyRequest, request_id)
    if not req:
        return jsonify({"error": "Emergency request not found."}), 404

    alerts = db_session.query(EmergencyAlert).filter_by(request_id=request_id)\
        .order_by(EmergencyAlert.created_at.desc()).all()

    return jsonify({
        "request_id": request_id,
        "alerts": [a.to_dict() for a in alerts]
    }), 200


@app.route("/api/hospitals", methods=["GET"])
@app.route("/api/hospitals/search", methods=["GET"])
@app.route("/api/hospitals/verify", methods=["GET"])
def list_hospitals():
    query = db_session.query(Hospital)

    # General search query across name, address, city, taluka, district, pincode, phone
    q = request.args.get("q") or request.args.get("search") or request.args.get("query")
    pincode_q = request.args.get("pincode")
    district_q = request.args.get("district") or request.args.get("dist")
    city_q = request.args.get("city") or request.args.get("village")
    taluka_q = request.args.get("taluka")
    name_q = request.args.get("name") or request.args.get("hospital_name")

    if q:
        clean_q = q.strip()
        search_filter = (
            Hospital.name.ilike(f"%{clean_q}%") |
            Hospital.address.ilike(f"%{clean_q}%") |
            Hospital.phone.ilike(f"%{clean_q}%") |
            Hospital.city.ilike(f"%{clean_q}%") |
            Hospital.taluka.ilike(f"%{clean_q}%") |
            Hospital.district.ilike(f"%{clean_q}%") |
            Hospital.pincode.ilike(f"%{clean_q}%")
        )
        query = query.filter(search_filter)

    if pincode_q:
        query = query.filter(Hospital.pincode.ilike(f"%{pincode_q.strip()}%"))
    if district_q:
        query = query.filter(Hospital.district.ilike(f"%{district_q.strip()}%"))
    if city_q:
        query = query.filter((Hospital.city.ilike(f"%{city_q.strip()}%")) | (Hospital.address.ilike(f"%{city_q.strip()}%")))
    if taluka_q:
        query = query.filter((Hospital.taluka.ilike(f"%{taluka_q.strip()}%")) | (Hospital.address.ilike(f"%{taluka_q.strip()}%")))
    if name_q:
        query = query.filter(Hospital.name.ilike(f"%{name_q.strip()}%"))

    hospitals = query.order_by(Hospital.name.asc()).all()

    lat_str = request.args.get("latitude") or request.args.get("lat")
    lng_str = request.args.get("longitude") or request.args.get("lng")

    user_lat = None
    user_lng = None
    if lat_str and lng_str:
        try:
            user_lat = float(lat_str)
            user_lng = float(lng_str)
        except ValueError:
            pass

    output = []
    for h in hospitals:
        hd = h.to_dict()
        if user_lat is not None and user_lng is not None and h.latitude is not None and h.longitude is not None:
            dist = calculate_haversine_distance(user_lat, user_lng, h.latitude, h.longitude)
            eta = calculate_ambulance_eta_minutes(dist, 50.0)
            hd["distance_km"] = dist
            hd["eta_minutes"] = eta
        else:
            hd["distance_km"] = None
            hd["eta_minutes"] = None
        output.append(hd)

    if user_lat is not None and user_lng is not None:
        output.sort(key=lambda x: (x["distance_km"] if x["distance_km"] is not None else 999))

    return jsonify({
        "hospitals": output,
        "count": len(output),
        "total_count": len(output),
        "query": q or "",
        "matched": len(output) > 0,
        "verified": len(output) > 0
    }), 200




# --- Frontend Page Routes ---

@app.route("/")
@app.route("/booking")
@app.route("/tracking")
@app.route("/login")
@app.route("/admin-login")
@app.route("/register")
@app.route("/dashboard")
@app.route("/driver")
@app.route("/hospital")
@app.route("/admin")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
