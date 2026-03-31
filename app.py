import firebase_admin
from firebase_admin import credentials, firestore, auth
from flask import (
    Flask, render_template, request, redirect,
    session, send_file, abort, jsonify, make_response, Response
)
import datetime
import os
import base64 as b64lib
import json

app = Flask(__name__)
app.secret_key = "parent_secret_key_2024"

# ── Firebase Init ──────────────────────────────────────────────────────
SERVICE_ACCOUNT_PATH = os.path.join(os.path.dirname(__file__), "serviceAccountKey.json")

cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred, {
    "databaseURL": "https://bus-management-660f7-default-rtdb.asia-southeast1.firebasedatabase.app",
    "storageBucket": "bus-management-660f7.firebasestorage.app"
})

db = firestore.client()

# ── Helpers ────────────────────────────────────────────────────────────
ADMIN_STATIC = os.path.join(os.path.dirname(__file__), "static")


def safe_json(obj):
    """
    Recursively convert any Firestore non-serializable types to JSON-safe primitives.
    Handles: DatetimeWithNanoseconds (Timestamp), GeoPoint, bytes, sets.
    """
    if isinstance(obj, dict):
        return {k: safe_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe_json(i) for i in obj]
    # Firestore Timestamp / Python datetime
    if hasattr(obj, 'strftime'):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    # Firestore GeoPoint
    if hasattr(obj, 'latitude') and hasattr(obj, 'longitude'):
        return {"lat": obj.latitude, "lng": obj.longitude}
    # bytes → skip (don't embed binary blobs in JSON)
    if isinstance(obj, (bytes, bytearray)):
        return None
    if isinstance(obj, set):
        return list(obj)
    return obj


def add_cors(response):
    """Allow cross-origin requests (needed when frontend is on a different domain)."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.after_request
def apply_cors(response):
    return add_cors(response)


@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    """Handle pre-flight CORS requests."""
    resp = make_response("", 204)
    return add_cors(resp)


def get_uid_from_request():
    """
    Extract and verify a Firebase ID token from:
    1. Authorization: Bearer <token> header  (production / deployed)
    2. Cookie named 'token' or 'student_token' (local dev fallback)
    Returns (uid, None) on success or (None, error_message) on failure.
    """
    token = None

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if not token:
        token = request.cookies.get("token") or request.cookies.get("student_token")

    if not token:
        return None, "Missing authentication token."

    try:
        decoded = auth.verify_id_token(token)
        return decoded.get("uid"), None
    except Exception as e:
        return None, f"Invalid or expired token: {e}"


def serve_image(rel_path):
    """Serve an image stored as a relative file path (from admin uploads)."""
    if not rel_path:
        abort(404)
    full_path = os.path.join(ADMIN_STATIC, rel_path.lstrip("/static/").lstrip("/"))
    if not os.path.exists(full_path):
        abort(404)
    return send_file(full_path)


def normalize_stops(stops):
    """
    Normalise a stops list from any schema variant:
      - Old subcollection schema: fields are name / lat / lng / latitude / longitude
      - New top-level routes schema: fields are stop_name / fee / coordinates
    Always outputs dicts with: name, fee, lat, lng (used by dashboard.html).
    """
    normalized = []
    for stop in stops:
        if not isinstance(stop, dict):
            continue
        raw = dict(stop)

        # ── Name: new schema uses 'stop_name', old uses 'name' ────────────
        name = raw.get("stop_name") or raw.get("name") or "N/A"

        # ── Fee ───────────────────────────────────────────────────────────
        fee = raw.get("fee")

        # ── Coordinates ──────────────────────────────────────────────────
        lat = raw.get("lat") or raw.get("latitude") or ""
        lng = raw.get("lng") or raw.get("longitude") or ""

        coords = raw.get("coordinates")
        if coords and not lat and not lng:
            if isinstance(coords, dict):
                lat = coords.get("lat") or coords.get("latitude") or ""
                lng = coords.get("lng") or coords.get("longitude") or ""
            elif hasattr(coords, "latitude"):   # Firestore GeoPoint
                lat = coords.latitude
                lng = coords.longitude
            elif isinstance(coords, str) and "," in coords:
                parts = coords.split(",", 1)
                try:
                    lat, lng = float(parts[0].strip()), float(parts[1].strip())
                except ValueError:
                    pass

        normalized.append({
            "name":      name,
            "fee":       fee,
            "radius":    raw.get("radius") or 0,
            "lat":       lat,
            "lng":       lng,
            "latitude":  lat,
            "longitude": lng,
        })
    return normalized


def _fetch_dashboard_data(uid):
    """
    Core data fetching logic shared by both the parent dashboard API.
    Returns a dict with all dashboard data or raises an Exception.
    """
    parent_doc = db.collection("parents").document(uid).get()
    if not parent_doc.exists:
        raise ValueError("No parent record found for this account.")

    parent_data = parent_doc.to_dict() or {}
    student_id = parent_data.get("studentId")

    if not student_id:
        raise ValueError("No student linked to this parent.")

    student_doc = db.collection("students").document(student_id).get()
    if not student_doc.exists:
        raise ValueError(f"Student record '{student_id}' does not exist.")

    student = student_doc.to_dict() or {}
    student_id = student_doc.id  # canonical ID

    return _build_response_data(student, student_id)


def _fetch_student_dashboard_data(uid):
    """
    Core data fetching logic for the student dashboard API.
    """
    students_query = (
        db.collection("students")
        .where("uid", "==", uid)
        .limit(1)
        .stream()
    )
    student_list = list(students_query)

    if not student_list:
        raise ValueError("Student not registered.")

    target_student = student_list[0]
    student_id = target_student.id
    student = target_student.to_dict() or {}

    return _build_response_data(student, student_id, include_history=True)


def _build_response_data(student, student_id, include_history=False):
    """
    Build the full dashboard response payload from a student dict.
    """
    # ── 1. Bus Details ─────────────────────────────────────────────────
    bus_data = None
    bus_id   = None
    driver   = None
    bus_no   = student.get("bus_no")

    if bus_no:
        bus_query = (
            db.collection("buses")
            .where("bus_number", "==", bus_no)
            .limit(1)
            .stream()
        )
        for b in bus_query:
            bus_id = b.id
            _d = dict(b.to_dict() or {})
            _d["_id"] = bus_id
            bus_data = _d

    # ── 2. Driver Details ──────────────────────────────────────────────
    if bus_data:
        driver_id = bus_data.get("assigned_driver_id")
        if driver_id:
            driver_doc = db.collection("drivers").document(driver_id).get()
            if driver_doc.exists:
                driver = driver_doc.to_dict() or {}

        cs = bus_data.get("current_strength")
        if (cs is None or cs == 0) and bus_id and bus_no:
            try:
                count = sum(1 for _ in db.collection("students").where("bus_no", "==", bus_no).limit(100).stream())
                bus_data["current_strength"] = count
            except Exception:
                bus_data["current_strength"] = "?"

    # Remove non-serializable base64 blobs from bus_data (served via separate endpoint)
    if bus_data:
        bus_data.pop("permitBase64", None)

    # Remove non-serializable base64 photo from student
    student_clean = {k: v for k, v in student.items() if k != "photoBase64"}

    # ── 3. Route Stops ─────────────────────────────────────────────────
    morning_stops = []
    evening_stops = []
    student_stop  = student.get("stop_name") or student.get("stop", "")

    route_fetched = False
    if bus_data:
        route_id = bus_data.get("route_id")
        if route_id:
            try:
                route_doc = db.collection("routes").document(str(route_id)).get()
                if route_doc.exists:
                    all_stops = normalize_stops(route_doc.to_dict().get("stops", []))
                    student_idx = next(
                        (i for i, s in enumerate(all_stops) if s.get("name") == student_stop), 0
                    )
                    morning_stops = all_stops[student_idx:]
                    route_fetched = True
            except Exception:
                pass

    if not route_fetched and bus_id:
        try:
            m_doc = db.collection("buses").document(bus_id).collection("routes").document("morning").get()
            if m_doc.exists:
                morning_stops = normalize_stops(m_doc.to_dict().get("stops", []))
        except Exception:
            pass
        try:
            e_doc = db.collection("buses").document(bus_id).collection("routes").document("evening").get()
            if e_doc.exists:
                evening_stops = normalize_stops(e_doc.to_dict().get("stops", []))
        except Exception:
            pass

    stop_fee = None
    for s in morning_stops:
        if s.get("name") == student_stop:
            stop_fee = s.get("fee")
            break

    # ── 4. Attendance ──────────────────────────────────────────────────
    attendance_today = None
    attendance_history = []
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        att_doc = (
            db.collection("attendance").document(today)
              .collection("students").document(student_id).get()
        )
        if att_doc.exists:
            attendance_today = att_doc.to_dict() or {}
    except Exception:
        pass

    if include_history:
        try:
            logs_query = (
                db.collection("attendance_logs")
                .where("student_id", "==", student_id)
                .order_by("timestamp", direction="DESCENDING")
                .limit(10)
                .stream()
            )
            for log in logs_query:
                ldict = log.to_dict() or {}
                ts = ldict.get("timestamp")
                if ts and hasattr(ts, "strftime"):
                    ldict["timestamp_str"] = ts.strftime("%Y-%m-%d %H:%M:%S")
                elif ts:
                    ldict["timestamp_str"] = str(ts)
                    ldict.pop("timestamp", None)
                attendance_history.append(ldict)
        except Exception as e:
            print(f"Error fetching attendance history: {e}")

    # ── 5. Fees ────────────────────────────────────────────────────────
    fees = []
    try:
        fees_query = (
            db.collection("fees")
            .where("studentId", "==", student_id)
            .stream()
        )
        for doc in fees_query:
            fee_data = doc.to_dict() or {}
            fee_data["id"] = doc.id
            # Convert any Firestore Timestamp objects to strings
            for k, v in fee_data.items():
                if hasattr(v, "strftime"):
                    fee_data[k] = v.strftime("%Y-%m-%d %H:%M:%S")
            fees.append(fee_data)
        fees.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    except Exception as e:
        print(f"Error fetching fees: {e}")

    result = {
        "student":            student_clean,
        "student_id":         student_id,
        "bus":                bus_data,
        "bus_id":             bus_id,
        "driver":             driver,
        "morning_stops":      morning_stops,
        "evening_stops":      evening_stops,
        "student_stop":       student_stop,
        "stop_fee":           stop_fee,
        "attendance":         attendance_today,
        "attendance_history": attendance_history,
        "fees":               fees,
        "photo_url":          f"/student-photo/{student_id}",
        "permit_url":         f"/bus-permit/{bus_id}" if bus_id else None,
    }
    # Deep-serialize to eliminate any Firestore Timestamps, GeoPoints, bytes etc.
    return safe_json(result)


# ── PAGE ROUTES (serve static HTML shells) ──────────────────────────────
@app.route("/", methods=["GET"])
def login():
    return render_template("login.html")


@app.route("/student-login", methods=["GET"])
def student_login():
    return render_template("student-login.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/student-dashboard")
def student_dashboard():
    return render_template("student_dashboard.html")


# ── API ROUTES ──────────────────────────────────────────────────────────
@app.route("/api/dashboard")
def api_dashboard():
    uid, err = get_uid_from_request()
    if err:
        return jsonify({"error": err}), 401

    try:
        data = _fetch_dashboard_data(uid)
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        print(f"Error in /api/dashboard: {e}")
        return jsonify({"error": f"Server error: {e}"}), 500


@app.route("/api/student-dashboard")
def api_student_dashboard():
    uid, err = get_uid_from_request()
    if err:
        return jsonify({"error": err}), 401

    try:
        data = _fetch_student_dashboard_data(uid)
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        print(f"Error in /api/student-dashboard: {e}")
        return jsonify({"error": f"Server error: {e}"}), 500


@app.route("/api/pay_fee/<fee_id>", methods=["POST", "GET"])
def api_pay_fee(fee_id):
    uid, err = get_uid_from_request()
    if err:
        return jsonify({"error": err}), 401

    try:
        decoded = auth.verify_id_token(
            request.headers.get("Authorization", "")[7:] or
            request.cookies.get("token", "")
        )
        email = decoded.get("email", "")
    except Exception:
        email = ""

    try:
        fee_ref = db.collection("fees").document(fee_id)
        fee_ref.update({
            "status": "paid",
            "paidAt": datetime.datetime.utcnow(),
            "paidBy": email
        })
        return jsonify({"success": True, "message": "Payment recorded."})
    except Exception as e:
        print(f"Error in /api/pay_fee: {e}")
        return jsonify({"error": f"Payment failed: {e}"}), 500


# ── IMAGE ROUTES  (serve admin-uploaded files as fallback) ─────────────
@app.route("/student-photo/<student_id>")
def student_photo(student_id):
    doc = db.collection("students").document(student_id).get()
    if not doc.exists:
        abort(404)
    data = doc.to_dict() or {}
    b64 = data.get("photoBase64", "")
    if b64:
        return _serve_base64_image(b64)
    path = data.get("photo_path", "")
    return serve_image(path)


@app.route("/bus-permit/<bus_id>")
def bus_permit(bus_id):
    doc = db.collection("buses").document(bus_id).get()
    if not doc.exists:
        abort(404)
    data = doc.to_dict() or {}
    b64 = data.get("permitBase64", "")
    if b64:
        return _serve_base64_image(b64)
    path = data.get("permit_photo", "")
    return serve_image(path)


def _serve_base64_image(b64_string: str) -> Response:
    mimetype = "image/jpeg"
    if b64_string.startswith("data:"):
        try:
            header, raw = b64_string.split(",", 1)
            declared = header.split(";")[0].split(":")[1]
            if declared.startswith("image/"):
                mimetype = declared
        except Exception:
            raw = b64_string
    else:
        raw = b64_string

    try:
        image_bytes = b64lib.b64decode(raw)
        return Response(image_bytes, mimetype=mimetype)
    except Exception:
        abort(404)


# ── LOGOUT ─────────────────────────────────────────────────────────────
@app.route("/logout")
def logout():
    resp = redirect("/")
    resp.set_cookie("token", "", expires=0)
    return resp


@app.route("/student-logout")
def student_logout():
    resp = redirect("/student-login")
    resp.set_cookie("student_token", "", expires=0)
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True)
