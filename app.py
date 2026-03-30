import firebase_admin
from firebase_admin import credentials, firestore, auth
from firebase_admin.firestore import FieldFilter
from flask import (
    Flask, render_template, request, redirect,
    session, send_file, abort, jsonify, make_response, Response
)
import datetime
import os
import base64 as b64lib

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

        # ── Coordinates: new schema may use a 'coordinates' dict/GeoPoint,
        #    old schema uses lat/lng or latitude/longitude directly ─────────
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


# ── LOGIN (GET only — POST handled client-side via Firebase Auth JS) ────
@app.route("/", methods=["GET", "POST"])
def login():
    if "parent_uid" in session:
        return redirect("/dashboard")
    elif "student_uid" in session:
        return redirect("/student-dashboard")
    return render_template("login.html")


# ── SESSION LOGIN (called by frontend after Firebase Auth) ─────────────
@app.route("/sessionLogin", methods=["POST"])
def session_login():
    """
    Frontend sends a Firebase ID token after successful signInWithEmailAndPassword.
    We verify it server-side and store the UID + email in Flask session.
    """
    data = request.get_json(silent=True) or {}
    id_token = data.get("idToken", "")

    if not id_token:
        return jsonify({"error": "Missing ID token"}), 400

    try:
        decoded = auth.verify_id_token(id_token)
        uid   = decoded["uid"]
        email = decoded.get("email", "")

        # Find the student linked to this parent email
        students = (
            db.collection("students")
            .where(filter=FieldFilter("parent_email", "==", email))
            .limit(10)
            .stream()
        )
        student_list = list(students)

        if not student_list:
            return jsonify({"error": "No student found for this account"}), 403

        # Store primary student (first match) in session
        primary_doc = student_list[0]
        session["parent_uid"]   = uid
        session["parent_email"] = email
        session["student_id"]   = primary_doc.id

        return jsonify({"status": "success", "redirect": "/dashboard"})

    except Exception as e:
        return jsonify({"error": f"Authentication failed: {str(e)}"}), 401


# ── STUDENT LOGIN endpoints ────────────────────────────────────────────
@app.route("/student-login", methods=["GET", "POST"])
def student_login():
    if "student_uid" in session:
        return redirect("/student-dashboard")
    elif "parent_uid" in session:
        return redirect("/dashboard")
    return render_template("student-login.html")


@app.route("/sessionStudentLogin", methods=["POST"])
def session_student_login():
    data = request.get_json(silent=True) or {}
    id_token = data.get("idToken", "")

    if not id_token:
        return jsonify({"error": "Missing ID token"}), 400

    try:
        decoded = auth.verify_id_token(id_token)
        uid   = decoded["uid"]
        email = decoded.get("email", "")

        # Find the student linked to this student UID
        students_query = (
            db.collection("students")
            .where(filter=FieldFilter("uid", "==", uid))
            .limit(1)
            .stream()
        )
        student_list = list(students_query)

        if not student_list:
            return jsonify({"error": "Student not registered"}), 403

        target_student = student_list[0]
        session.clear()
        session["student_uid"]   = uid
        session["student_email"] = email
        session["student_doc_id"]   = target_student.id

        return jsonify({"status": "success", "redirect": "/student-dashboard"})

    except Exception as e:
        return jsonify({"error": f"Authentication failed: {str(e)}"}), 401


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
    """
    Decode a base64 image string (with or without data URI prefix)
    and return it as a proper image Response with correct Content-Type.
    """
    mimetype = "image/jpeg"  # safe default

    if b64_string.startswith("data:"):
        # Format: "data:image/png;base64,<data>"
        try:
            header, raw = b64_string.split(",", 1)
            declared = header.split(";")[0].split(":")[1]  # e.g. "image/png"
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


# ── DASHBOARD ──────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    # Enforce parent access only
    if "parent_uid" not in session:
        return redirect("/")

    # Ensure backward compatibility with old sessions if student_id is missing somehow
    if "student_id" not in session:
        return redirect("/")

    student_id  = session["student_id"]
    student_doc = db.collection("students").document(student_id).get()

    if not student_doc.exists:
        session.clear()
        return redirect("/")

    student    = student_doc.to_dict() or {}
    student_id = student_doc.id  # canonical ID

    # ── 1. Bus Details ─────────────────────────────────────────────────
    bus_data   = None
    bus_id     = None
    driver     = None
    bus_no     = student.get("bus_no")

    if bus_no:
        bus_query = (
            db.collection("buses")
            .where(filter=FieldFilter("bus_number", "==", bus_no))
            .limit(1)
            .stream()
        )
        for b in bus_query:
            bus_id  = b.id
            _d      = dict(b.to_dict() or {})  # explicit plain dict
            _d["_id"] = bus_id
            bus_data = _d

    # ── 2. Driver Details ──────────────────────────────────────────────
    if bus_data:
        driver_id = bus_data.get("assigned_driver_id")
        if driver_id:
            driver_doc = db.collection("drivers").document(driver_id).get()
            if driver_doc.exists:
                driver = driver_doc.to_dict() or {}

        # Bus capacity: if current_strength is 0 or None, count dynamically
        cs = bus_data.get("current_strength") if bus_data else None
        if (cs is None or cs == 0) and bus_id and bus_no:
            try:
                count_query = (
                    db.collection("students")
                    .where(filter=FieldFilter("bus_no", "==", bus_no))
                    .limit(100)
                    .stream()
                )
                count = sum(1 for _ in count_query)
                if isinstance(bus_data, dict):
                    bus_data["current_strength"] = count
            except Exception:
                if isinstance(bus_data, dict):
                    bus_data["current_strength"] = "?"

    # ── 3. Route Stops ─────────────────────────────────────────────────
    # Strategy:
    #   a) Try new schema: students.stop_name → buses.route_id → routes/{id}
    #   b) Fall back to old subcollection schema: buses/{id}/routes/morning|evening
    morning_stops = []
    evening_stops = []

    # student_stop: prefer stop_name (new schema), fall back to stop (old)
    student_stop = student.get("stop_name") or student.get("stop", "")

    # ── 3a. New schema: top-level routes collection ─────────────────────
    route_fetched = False
    if bus_data:
        route_id = bus_data.get("route_id")
        if route_id:
            try:
                route_doc = db.collection("routes").document(str(route_id)).get()
                if route_doc.exists:
                    all_stops = normalize_stops(route_doc.to_dict().get("stops", []))

                    # Find student's stop index and slice from there
                    student_idx = next(
                        (i for i, s in enumerate(all_stops)
                         if s.get("name") == student_stop),
                        0  # default: show all if stop not matched
                    )
                    morning_stops = all_stops[student_idx:]
                    route_fetched = True
            except Exception:
                pass

    # ── 3b. Old subcollection schema fallback ───────────────────────────
    if not route_fetched and bus_id:
        try:
            m_doc = (
                db.collection("buses").document(bus_id)
                  .collection("routes").document("morning").get()
            )
            if m_doc.exists:
                morning_stops = normalize_stops(m_doc.to_dict().get("stops", []))
        except Exception:
            pass

        try:
            e_doc = (
                db.collection("buses").document(bus_id)
                  .collection("routes").document("evening").get()
            )
            if e_doc.exists:
                evening_stops = normalize_stops(e_doc.to_dict().get("stops", []))
        except Exception:
            pass

    # Find fee for student's stop
    stop_fee = None
    for s in morning_stops:
        if s.get("name") == student_stop:
            stop_fee = s.get("fee")
            break

    # ── 4. Attendance (today) ──────────────────────────────────────────
    attendance_today = None
    try:
        today   = datetime.datetime.now().strftime("%Y-%m-%d")
        att_doc = (
            db.collection("attendance").document(today)
              .collection("students").document(student_id).get()
        )
        if att_doc.exists:
            attendance_today = att_doc.to_dict() or {}
    except Exception:
        pass

    # ── 5. Fetch Fees (from 'fees' collection) ─────────────────────────
    fees = []
    try:
        fees_query = (
            db.collection("fees")
            .where(filter=FieldFilter("studentId", "==", student_id))
            .stream()
        )
        for doc in fees_query:
            fee_data = doc.to_dict() or {}
            fee_data["id"] = doc.id
            fees.append(fee_data)
        
        # Sort by createdAt descending
        fees.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    except Exception as e:
        print(f"Error fetching fees: {e}")

    # ── 6. Base64 image flags (passed to template) ─────────────────────
    photo_b64  = student.get("photoBase64", "")
    permit_b64 = bus_data.get("permitBase64", "") if bus_data else ""

    return render_template(
        "dashboard.html",
        student       = student,
        student_id    = student_id,
        bus           = bus_data,
        bus_id        = bus_id,
        driver        = driver,
        morning_stops = morning_stops,
        evening_stops = evening_stops,
        student_stop  = student_stop,
        stop_fee      = stop_fee,
        attendance    = attendance_today,
        fees          = fees,
        photo_b64     = photo_b64,
        permit_b64    = permit_b64,
    )


# ── STUDENT DASHBOARD ──────────────────────────────────────────────────
@app.route("/student-dashboard")
def student_dashboard():
    if "student_uid" not in session:
        return redirect("/student-login")

    student_id  = session.get("student_doc_id")
    if not student_id:
        session.clear()
        return redirect("/student-login")

    student_doc = db.collection("students").document(student_id).get()

    if not student_doc.exists:
        session.clear()
        return redirect("/student-login")

    student    = student_doc.to_dict() or {}

    # ── 1. Bus Details ─────────────────────────────────────────────────
    bus_data   = None
    bus_id     = None
    driver     = None
    bus_no     = student.get("bus_no")

    if bus_no:
        bus_query = (
            db.collection("buses")
            .where(filter=FieldFilter("bus_number", "==", bus_no))
            .limit(1)
            .stream()
        )
        for b in bus_query:
            bus_id  = b.id
            _d      = dict(b.to_dict() or {})
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
                count_query = (
                    db.collection("students")
                    .where(filter=FieldFilter("bus_no", "==", bus_no))
                    .limit(100)
                    .stream()
                )
                count = sum(1 for _ in count_query)
                if isinstance(bus_data, dict):
                    bus_data["current_strength"] = count
            except Exception:
                if isinstance(bus_data, dict):
                    bus_data["current_strength"] = "?"

    # ── 3. Route Stops ─────────────────────────────────────────────────
    morning_stops = []
    evening_stops = []

    student_stop = student.get("stop_name") or student.get("stop", "")

    # 3a. New schema: top-level routes collection
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

    # 3b. Old subcollection schema fallback
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

    # ── 4. Attendance (today) ──────────────────────────────────────────
    attendance_today = None
    attendance_history = []
    try:
        today   = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # Today's scan
        att_doc = db.collection("attendance").document(today).collection("students").document(student_id).get()
        if att_doc.exists:
            attendance_today = att_doc.to_dict() or {}
            
        # Overall history query (last 10 scans)
        logs_query = (
            db.collection("attendance_logs")
            .where(filter=FieldFilter("student_id", "==", student_id))
            .order_by("timestamp", direction="DESCENDING")
            .limit(10)
            .stream()
        )
        for log in logs_query:
            ldict = log.to_dict() or {}
            if "timestamp" in ldict and hasattr(ldict["timestamp"], "strftime"):
                ldict["timestamp_str"] = ldict["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            elif "timestamp" in ldict:
                ldict["timestamp_str"] = str(ldict["timestamp"])
            attendance_history.append(ldict)
    except Exception as e:
        print(f"Error fetching attendance: {e}")

    # ── 5. Fetch Fees (from 'fees' collection) ─────────────────────────
    fees = []
    try:
        fees_query = (
            db.collection("fees")
            .where(filter=FieldFilter("studentId", "==", student_id))
            .stream()
        )
        for doc in fees_query:
            fee_data = doc.to_dict() or {}
            fee_data["id"] = doc.id
            fees.append(fee_data)
        
        fees.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    except Exception as e:
        print(f"Error fetching fees: {e}")

    photo_b64  = student.get("photoBase64", "")
    permit_b64 = bus_data.get("permitBase64", "") if bus_data else ""

    return render_template(
        "student_dashboard.html",
        student       = student,
        student_id    = student_id,
        bus           = bus_data,
        bus_id        = bus_id,
        driver        = driver,
        morning_stops = morning_stops,
        evening_stops = evening_stops,
        student_stop  = student_stop,
        stop_fee      = stop_fee,
        attendance    = attendance_today,
        attendance_history = attendance_history,
        fees          = fees,
        photo_b64     = photo_b64,
        permit_b64    = permit_b64,
    )


# ── PAY FEE ────────────────────────────────────────────────────────────
@app.route("/pay_fee/<fee_id>")
def pay_fee(fee_id):
    if "parent_uid" not in session:
        return redirect("/")

    try:
        fee_ref = db.collection("fees").document(fee_id)
        
        # Update
        update_data = {
            "status": "paid",
            "paidAt": datetime.datetime.utcnow(),
            "paidBy": session.get("parent_email", "")
        }
            
        fee_ref.update(update_data)
            
    except Exception as e:
        print(f"Error in /pay_fee: {e}")
        return f"Payment update failed: {e}", 500

    return redirect("/dashboard")


# ── LOGOUT ─────────────────────────────────────────────────────────────
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/student-logout")
def student_logout():
    session.clear()
    return redirect("/student-login")


# ── ERROR HANDLERS ─────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(_):
    return render_template("login.html", error="Page not found."), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("login.html", error=f"Server error: {e}"), 500


if __name__ == "__main__":
    app.run(debug=True, port=5050)
