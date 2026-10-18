# RapidAid — Real-Time Emergency Ambulance Dispatch & Tracking System

RapidAid is a full-stack, production-ready emergency medical response platform designed to rapidly connect patients in critical need with nearby available ambulances, stream live GPS telemetry to patients and dispatch administrators, provide real-time ETA estimates, and provide administrative dispatch control.

---

## 🌟 Key Features

### 🚑 Patient / User Portal
* **User Authentication & Session Management**: Secure user registration, login, logout, and session persistence.
* **Emergency Request Booking**: Emergency type selection (cardiac, trauma, maternity, general, ICU), ambulance type selection (ALS, BLS, NEONATAL, ICU), and coordinate-based pickup address submission.
* **Automatic Ambulance Matching**: Real-time Haversine geographic distance evaluation algorithm automatically matching the nearest available suitable ambulance.
* **Live GPS Tracking**: Real-time Leaflet map tracking of assigned ambulance movements via Server-Sent Events (SSE).
* **ETA & Telemetry Freshness**: Real-time distance and arrival time calculation with stale location indicators (>120s).
* **Request Lifecycle Management**: Active request status tracking (REQUESTED, ASSIGNED, ACCEPTED, CANCELLED, COMPLETED) with ownership isolation.

### 🚘 Driver Portal
* **Driver Authentication & Authorization**: Dedicated role-based access for driver accounts linked to specific vehicle units.
* **Vehicle & Assignment Dashboard**: Real-time view of assigned vehicle unit, active emergency requests, and assignment history.
* **Accept / Reject Workflow**: Accept assigned emergencies (transitions status to ACCEPTED) or reject to release unit back to AVAILABLE status.
* **HTML5 Geolocation Telemetry Streaming**: High-accuracy browser GPS location collection with throttling and automatic backend telemetry synchronization.

### 🛡️ Admin Command Center
* **Operational Overview & Metrics**: Real-time statistics across total fleet, active trips, driver accounts, and patient requests.
* **Fleet Management**: Interactive fleet directory with unit status toggling (AVAILABLE, BUSY, OFFLINE) and safety locks preventing marking units OFFLINE while assigned to active trips.
* **Active Trip Monitoring**: Live dispatch monitor tracking active emergency responses, patient pickup locations, assigned vehicles, and real-time telemetry.
* **User & Driver Directories**: Administrative management and history views for all platform users.

---

## 🏗️ System Architecture

```text
+-------------------------------------------------------------+
|                      Frontend SPA                           |
|       (HTML5, Vanilla CSS, JavaScript, Leaflet.js)          |
+-------------------------------------------------------------+
                              │
                    REST API & SSE Stream
                              │
                              ▼
+-------------------------------------------------------------+
|                     Flask Backend API                       |
|  - Auth Middleware & RBAC (@driver_required, @admin_required)|
|  - Matching Engine (services/matching.py)                   |
|  - ETA & Freshness Engine (services/eta.py)                 |
|  - Health & Security Headers (GET /api/health)              |
+-------------------------------------------------------------+
                              │
                         SQLAlchemy ORM
                              │
                              ▼
+-------------------------------------------------------------+
|                 Database Layer (SQLAlchemy)                 |
|   Development: SQLite (rapidaid.db)                         |
|   Production:  PostgreSQL (via DATABASE_URL env)            |
+-------------------------------------------------------------+
```

---

## ⚙️ Prerequisites & Dependencies

* **Python**: 3.10 or higher
* **Database**: PostgreSQL 13+ (Production) or SQLite 3 (Local Development)
* **Python Libraries**: `Flask`, `SQLAlchemy`, `Werkzeug`, `python-dotenv`, `httpx`

---

## 🛠️ Environment Configuration

Copy `.env.example` to `.env` and update parameters as required:

```bash
cp .env.example .env
```

| Variable | Description | Default / Example |
| -------- | ----------- | ----------------- |
| `DATABASE_URL` | SQLAlchemy connection string | `sqlite:///rapidaid.db` or `postgresql://user:pass@localhost:5432/rapidaid` |
| `SECRET_KEY` | Flask session encryption secret | `rapidaid_fallback_secret_key_2026` |
| `PORT` | Server listening port | `5000` |
| `FLASK_ENV` | Application mode | `production` or `development` |
| `SECURE_COOKIES` | Enforce HTTPS Secure flag on session cookies | `false` (set `true` in HTTPS production) |
| `ROUTING_PROVIDER_URL` | Optional external routing engine API URL | `""` |

---

## 🚀 Local Quickstart Guide

### 1. Clone & Set Up Virtual Environment

```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Initialize & Seed Database

```bash
# Run database seed script to create initial schema and demo accounts
python seed_db.py
```

### 3. Start Application Server

```bash
python app.py
```

Open your browser and navigate to `http://127.0.0.1:5000`.

---

## 🔑 Default Demo Accounts (Development)

| Role | Email | Password | Linked Unit |
| ---- | ----- | -------- | ----------- |
| **Patient / User** | `user@rapidaid.org` | `Password123!` | N/A |
| **Driver** | `driver@rapidaid.org` | `Password123!` | AMB-101 (ALS) |
| **Admin** | `admin@rapidaid.org` | `Password123!` | N/A |

---

## 🧪 Automated Testing

Run the full automated test suite containing 85 comprehensive unit, integration, and security tests:

```bash
python -m unittest discover -s tests
```

### Final Verified Test Results:

```text
Ran 85 tests in 72.571s

OK (PASS: 85, FAIL: 0, ERROR: 0, SKIPPED: 0)
```

---

## 🏥 Health & Diagnostics Endpoint

RapidAid exposes a production-safe health monitor at:

```text
GET /api/health
```

### Sample Response (Healthy):

```json
{
  "status": "healthy",
  "database": "connected",
  "timestamp": "2026-10-08T20:46:00.000000+00:00"
}
```

---

## 🔒 Security Audit & Best Practices

1. **Password Protection**: Passwords hashed using Werkzeug `generate_password_hash` (scrypt/pbkdf2). Hashes are sanitized out of all API responses (`to_dict()` excludes credentials).
2. **Access Control (RBAC)**: Enforced at backend API layer using `@driver_required` and `@admin_required` decorators.
3. **Session Hardening**: Cookies configured with `HttpOnly=True` and `SameSite=Lax`.
4. **Security Headers**: Standard headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `X-XSS-Protection: 1; mode=block`, `Referrer-Policy: strict-origin-when-cross-origin`) attached to all responses.
5. **HTTPS Requirement**: HTML5 Geolocation API requires HTTPS in production environments.

---

## 🌐 Production Deployment Guide

1. **Database Setup**:
   Create a PostgreSQL database and set `DATABASE_URL=postgresql://user:pass@host:5432/dbname` in environment variables.
2. **WSGI Server**:
   Deploy using Gunicorn or uWSGI:
   ```bash
   gunicorn -w 4 -b 0.0.0.0:5000 app:app
   ```
3. **Reverse Proxy & SSL**:
   Use Nginx / Caddy as a reverse proxy terminating HTTPS for secure location tracking cookies.

---

## 👥 Development Group — Pixel_5

**Pixel_5** is the engineering team behind the design, architecture, and development of RapidAid:

* **Nilesh Raut** — *Team Leader & Lead Developer*  
  Project leadership, application development, technical implementation, and system design.
* **Parth Chaudhari** — *Co-Leader & Communication Lead*  
  Team coordination, communication, collaboration, and project organization.
* **Pratik Gajare** — *Associate Developer*  
  Feature development, technical implementation, and development support.
* **Dhanraj Sargar** — *Presentation & Narration*  
  Project presentation, feature explanation, and project demonstration narration.
* **Yashraj Markad** — *Presentation & Narration*  
  Project explanation, presentation support, and narration of application functionality.

