import os
import datetime
import firebase_admin
from firebase_admin import credentials, firestore, auth as fb_auth
from flask import Flask, render_template, request, jsonify, make_response, send_file, abort

# ──────────────────────────────────────────────────────────
#  App setup
# ──────────────────────────────────────────────────────────
app = Flask(__name__)

SERVICE_ACCOUNT_PATH = os.path.join(os.path.dirname(__file__), "serviceAccountKey.json")
cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred, {
    "storageBucket": "bus-management-660f7.firebasestorage.app"
})
db = firestore.client()
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# ──────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────

def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.after_request
def add_cors(r):
    return cors(r)

@app.route("/api/<path:p>", methods=["OPTIONS"])
def preflight(p):
    return cors(make_response("", 204))


def safe(obj):
    """Recursively convert Firestore-specific types to JSON-safe primitives."""
    if isinstance(obj, dict):
        return {k: safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe(i) for i in obj]
    if hasattr(obj, "strftime"):           # Timestamp / datetime
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(obj, "latitude"):           # GeoPoint
        return {"lat": obj.latitude, "lng": obj.longitude}
    if isinstance(obj, (bytes, bytearray)):
        return None
    if isinstance(obj, set):
        return list(obj)
    return obj


def verify_token():
    """
    Extract Firebase UID from Authorization header.
    Returns (uid, None) on success, (None, error_str) on failure.
    Logs all debug info.
    """
    auth_header = request.headers.get("Authorization", "")
    print(f"[AUTH] Header: '{auth_header[:40]}...' | Path: {request.path}")

    if not auth_header.startswith("Bearer "):
        print("[AUTH] FAIL – no Bearer header")
        return None, "Missing Authorization header. Please log in again."

    token = auth_header[7:].strip()
    if not token:
        print("[AUTH] FAIL – empty token")
        return None, "Token is empty."

    try:
        decoded = fb_auth.verify_id_token(token)
        uid = decoded["uid"]
        print(f"[AUTH] OK – uid={uid}")
        return uid, None
    except Exception as e:
        print(f"[AUTH] FAIL – {type(e).__name__}: {e}")
        return None, f"Token invalid or expired. Please log in again. ({type(e).__name__})"


def normalize_stops(stops):
    """Normalize stop dicts from any schema variant."""
    out = []
    for s in stops:
        if not isinstance(s, dict):
            continue
        name = s.get("stop_name") or s.get("name") or "N/A"
        fee  = s.get("fee")
        lat  = s.get("lat") or s.get("latitude") or ""
        lng  = s.get("lng") or s.get("longitude") or ""
        coords = s.get("coordinates")
        if coords and not lat:
            if hasattr(coords, "latitude"):
                lat, lng = coords.latitude, coords.longitude
            elif isinstance(coords, dict):
                lat = coords.get("lat") or coords.get("latitude") or ""
                lng = coords.get("lng") or coords.get("longitude") or ""
        out.append({"name": name, "fee": fee, "lat": lat, "lng": lng})
    return out


def get_student_data(student_id):
    """Fetch all data for a given student document ID. Returns safe dict."""
    student_doc = db.collection("students").document(student_id).get()
    if not student_doc.exists:
        raise ValueError(f"Student record not found: {student_id}")

    student = dict(student_doc.to_dict() or {})
    student_stop = student.get("stop_name") or student.get("stopName") or ""
    bus_id = student.get("busId") or student.get("bus_id") or ""

    # ── Bus ──────────────────────────────────────────────
    bus_data = None
    driver_data = None
    morning_stops = []
    evening_stops = []
    permit_url = None

    if bus_id:
        bus_doc = db.collection("buses").document(bus_id).get()
        if bus_doc.exists:
            bus_data = dict(bus_doc.to_dict() or {})
            # Count students
            try:
                count = db.collection("students").where("busId", "==", bus_id).count().get()
                bus_data["current_strength"] = count[0][0].value
            except Exception:
                bus_data["current_strength"] = 0

            # Driver
            driver_id = bus_data.get("driverId") or bus_data.get("driver_id")
            if driver_id:
                drv = db.collection("drivers").document(driver_id).get()
                if drv.exists:
                    driver_data = dict(drv.to_dict() or {})

            # Permit image
            permit_path = bus_data.get("permitImage") or bus_data.get("permit_image")
            if permit_path:
                permit_url = f"/bus-permit/{bus_id}"

            # Route stops (try subcollection first, then top-level)
            try:
                m = db.collection("buses").document(bus_id).collection("routes").document("morning").get()
                if m.exists:
                    morning_stops = normalize_stops(m.to_dict().get("stops", []))
            except Exception:
                pass
            try:
                e = db.collection("buses").document(bus_id).collection("routes").document("evening").get()
                if e.exists:
                    evening_stops = normalize_stops(e.to_dict().get("stops", []))
            except Exception:
                pass

    # ── Today's attendance ───────────────────────────────
    attendance_today = None
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        att = db.collection("attendance").document(today).collection("students").document(student_id).get()
        if att.exists:
            attendance_today = dict(att.to_dict() or {})
    except Exception:
        pass

    # ── Attendance history (last 10) ─────────────────────
    att_history = []
    try:
        hist = (db.collection("attendance_history")
                  .where("studentId", "==", student_id)
                  .order_by("timestamp", direction=firestore.Query.DESCENDING)
                  .limit(10).get())
        for h in hist:
            d = dict(h.to_dict() or {})
            ts = d.get("timestamp")
            d["timestamp_str"] = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts or "")
            att_history.append(d)
    except Exception:
        pass

    # ── Fees ─────────────────────────────────────────────
    fees = []
    try:
        fee_docs = db.collection("fees").where("studentId", "==", student_id).get()
        for f in fee_docs:
            fd = dict(f.to_dict() or {})
            fd["id"] = f.id
            # Serialize timestamps
            for k in ("createdAt", "paidAt", "dueDate"):
                v = fd.get(k)
                if hasattr(v, "strftime"):
                    fd[k] = v.strftime("%Y-%m-%d")
            fees.append(fd)
        fees.sort(key=lambda x: x.get("createdAt") or "", reverse=True)
    except Exception as e:
        print(f"[FEES] Error: {e}")

    result = {
        "student":            student,
        "student_id":         student_id,
        "bus":                bus_data,
        "bus_id":             bus_id,
        "driver":             driver_data,
        "morning_stops":      morning_stops,
        "evening_stops":      evening_stops,
        "student_stop":       student_stop,
        "attendance":         attendance_today,
        "attendance_history": att_history,
        "fees":               fees,
        "photo_url":          f"/student-photo/{student_id}",
        "permit_url":         permit_url,
    }
    return safe(result)


# ──────────────────────────────────────────────────────────
#  Page routes (serve HTML shells)
# ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("login.html")

@app.route("/student-login")
def student_login_page():
    return render_template("student-login.html")

@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")

@app.route("/student-dashboard")
def student_dashboard_page():
    return render_template("student_dashboard.html")

@app.route("/logout")
def logout():
    return render_template("login.html")

@app.route("/student-logout")
def student_logout():
    return render_template("student-login.html")


# ──────────────────────────────────────────────────────────
#  Debug / health endpoints
# ──────────────────────────────────────────────────────────

@app.route("/api/ping")
def api_ping():
    """Quick health check — no auth required."""
    return jsonify({"status": "ok", "time": datetime.datetime.now().isoformat()})

@app.route("/api/whoami")
def api_whoami():
    """Return the UID from the token — useful for confirming auth works."""
    uid, err = verify_token()
    if err:
        return jsonify({"error": err}), 401
    return jsonify({"uid": uid})


# ──────────────────────────────────────────────────────────
#  Parent dashboard API
# ──────────────────────────────────────────────────────────

@app.route("/api/dashboard")
def api_dashboard():
    uid, err = verify_token()
    if err:
        return jsonify({"error": err}), 401

    try:
        print(f"[DASHBOARD] Fetching parent record for uid={uid}")
        parent_doc = db.collection("parents").document(uid).get()
        if not parent_doc.exists:
            return jsonify({"error": "No parent account found. Please contact admin."}), 403

        parent = parent_doc.to_dict() or {}
        student_id = parent.get("studentId") or parent.get("student_id")
        if not student_id:
            return jsonify({"error": "No student linked to your account. Please contact admin."}), 403

        print(f"[DASHBOARD] Found studentId={student_id}")
        data = get_student_data(student_id)
        return jsonify(data)

    except ValueError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        import traceback
        print(f"[DASHBOARD ERROR] {e}\n{traceback.format_exc()}")
        return jsonify({"error": f"Server error: {e}"}), 500


# ──────────────────────────────────────────────────────────
#  Student dashboard API
# ──────────────────────────────────────────────────────────

@app.route("/api/student-dashboard")
def api_student_dashboard():
    uid, err = verify_token()
    if err:
        return jsonify({"error": err}), 401

    try:
        print(f"[STUDENT-DASH] Fetching student record for uid={uid}")
        # Find student by Firebase UID
        results = db.collection("students").where("uid", "==", uid).limit(1).get()
        if not results:
            results = db.collection("students").where("userId", "==", uid).limit(1).get()
        if not results:
            results = db.collection("students").where("firebaseUid", "==", uid).limit(1).get()

        if not results:
            return jsonify({"error": "No student account found for this login. Please contact admin."}), 403

        student_id = results[0].id
        print(f"[STUDENT-DASH] Found student_id={student_id}")
        data = get_student_data(student_id)
        return jsonify(data)

    except ValueError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        import traceback
        print(f"[STUDENT-DASH ERROR] {e}\n{traceback.format_exc()}")
        return jsonify({"error": f"Server error: {e}"}), 500


# ──────────────────────────────────────────────────────────
#  Fee payment
# ──────────────────────────────────────────────────────────

@app.route("/api/pay_fee/<fee_id>", methods=["POST", "GET"])
def api_pay_fee(fee_id):
    uid, err = verify_token()
    if err:
        return jsonify({"error": err}), 401

    try:
        fee_ref = db.collection("fees").document(fee_id)
        fee_doc = fee_ref.get()
        if not fee_doc.exists:
            return jsonify({"error": "Fee record not found."}), 404

        fee_ref.update({
            "status": "paid",
            "paidAt": firestore.SERVER_TIMESTAMP,
            "paidBy": uid,
        })
        return jsonify({"success": True, "message": "Fee paid successfully."})
    except Exception as e:
        print(f"[PAY_FEE ERROR] {e}")
        return jsonify({"error": f"Payment failed: {e}"}), 500


# ──────────────────────────────────────────────────────────
#  Image serving
# ──────────────────────────────────────────────────────────

@app.route("/student-photo/<student_id>")
def student_photo(student_id):
    try:
        doc = db.collection("students").document(student_id).get()
        if not doc.exists:
            abort(404)
        photo = (doc.to_dict() or {}).get("photo") or (doc.to_dict() or {}).get("photoUrl")
        if not photo:
            abort(404)
        path = os.path.join(STATIC_DIR, photo.lstrip("/static/").lstrip("/"))
        if os.path.exists(path):
            return send_file(path)
    except Exception:
        pass
    abort(404)


@app.route("/bus-permit/<bus_id>")
def bus_permit(bus_id):
    try:
        doc = db.collection("buses").document(bus_id).get()
        if not doc.exists:
            abort(404)
        permit = (doc.to_dict() or {}).get("permitImage") or (doc.to_dict() or {}).get("permit_image")
        if not permit:
            abort(404)
        path = os.path.join(STATIC_DIR, permit.lstrip("/static/").lstrip("/"))
        if os.path.exists(path):
            return send_file(path)
    except Exception:
        pass
    abort(404)


# ──────────────────────────────────────────────────────────
#  Global error handlers — always return JSON for /api/ routes
# ──────────────────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    print(f"[UNHANDLED] {type(e).__name__}: {e}\n{traceback.format_exc()}")
    if request.path.startswith("/api/"):
        return jsonify({"error": str(e)}), 500
    return make_response(f"<h2>500 – {e}</h2>", 500)

@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return make_response("<h2>404 – Page not found</h2>", 404)


# ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)
