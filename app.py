from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import gspread
from google.oauth2.service_account import Credentials
import os

# ---------------------- Config ----------------------
# Path to local service account key file in project folder
SERVICE_ACCOUNT_FILE = os.environ.get(
    "GSA_KEY_FILE", "synthetic-trail-467009-e8-876a545fffbd.json"
)

# Your Google Sheet details
SHEET_ID = "1xV2S1xxiomM5Mj5EorzXUkWD0jhlBhFOvqqPxwJuiec"
USERS_SHEET_NAME = "users"

# Flask app setup
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Google Sheets scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ----------------- Google Sheets helpers -----------------
def get_gspread_client():
    """Authorize gspread using local service account JSON file."""
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError(f"Service account file not found: {SERVICE_ACCOUNT_FILE}")
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return gspread.authorize(creds)

def get_worksheet(sheet_name):
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        raise RuntimeError(f"Worksheet '{sheet_name}' not found in the spreadsheet")

def fetch_user_by_email(email):
    """Fetch user details by email."""
    ws = get_worksheet(USERS_SHEET_NAME)
    rows = ws.get_all_values()
    if not rows:
        return None

    headers = [h.strip().lower() for h in rows[0]]
    try:
        idx_email = headers.index("emailid")
        idx_password = headers.index("password")
        idx_role = headers.index("role")
        idx_status = headers.index("status")
    except ValueError:
        raise RuntimeError("Sheet must have headers: name | emailid | role | mobile | designation | status | password")

    for r in rows[1:]:
        if len(r) <= idx_email:
            continue
        if r[idx_email].strip().lower() == email.strip().lower():
            return {
                "email": email.strip(),
                "password": r[idx_password].strip() if len(r) > idx_password else "",
                "role": r[idx_role].strip().lower() if len(r) > idx_role else "",
                "status": r[idx_status].strip().lower() if len(r) > idx_status else ""
            }
    return None

def fetch_all_employees():
    """Fetch all employees from the Google Sheet."""
    ws = get_worksheet(USERS_SHEET_NAME)
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return []
    headers = [h.strip().lower() for h in rows[0]]
    employees = []
    for r in rows[1:]:
        emp = {}
        for i, h in enumerate(headers):
            emp[h] = r[i].strip() if i < len(r) else ""
        employees.append(emp)
    return employees

# ----------------- Auth decorators -----------------
from functools import wraps

def login_required(required_role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = session.get("user")
            if not user:
                flash("Please log in to continue.", "info")
                return redirect(url_for("login"))
            if required_role and user.get("role") != required_role:
                flash("Insufficient permissions for this page.", "error")
                return redirect(url_for("home"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# ----------------- Routes -----------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for("login"))

        try:
            user = fetch_user_by_email(email)
        except Exception as e:
            flash(f"Authentication error: {e}", "error")
            return redirect(url_for("login"))

        if not user or user["password"] != password:
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))

        if user["status"] != "active":
            flash("Account is inactive. Contact administrator.", "error")
            return redirect(url_for("login"))

        if user["role"] not in ("admin", "reviewer"):
            flash("Unauthorized role.", "error")
            return redirect(url_for("login"))

        session["user"] = {"email": user["email"], "role": user["role"]}
        session.permanent = False

        if user["role"] == "admin":
            return redirect(url_for("admin_dashboard"))
        else:
            return redirect(url_for("employee_dashboard"))

    return render_template("login.html")

@app.get("/home")
@login_required()
def home():
    u = session["user"]
    return f"Welcome {u['email']}! Your role is {u['role']}."

@app.get("/admin")
@login_required(required_role="admin")
def admin_dashboard():
    employees = fetch_all_employees()
    user = session.get("user", {})
    admin_info = next((e for e in employees if e.get("emailid", "").lower() == user.get("email", "").lower()), None)
    return render_template("admin_dashboard.html", employees=employees, user=admin_info)

@app.get("/employee")
@login_required(required_role="employee")
def employee_dashboard():
    u = session["user"]
    return f"Employee dashboard. User: {u['email']}."

@app.get("/logout")
def logout():
    session.pop("user", None)
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))

@app.route("/admin/add_employee", methods=["POST"])
@login_required(required_role="admin")
def add_employee():
    name = request.form.get("name", "").strip()
    emailid = request.form.get("emailid", "").strip().lower()
    role = request.form.get("role", "").strip().capitalize()  # "Admin" or "Reviewer"
    password = request.form.get("password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()
    mobile = request.form.get("mobile", "").strip()
    designation = request.form.get("designation", "").strip()
    status = request.form.get("status", "Active").strip().capitalize()

    if not all([name, emailid, role, password, confirm_password]):
        flash("All required fields must be filled.", "error")
        return redirect(url_for('admin_dashboard'))

    if password != confirm_password:
        flash("Passwords do not match.", "error")
        return redirect(url_for('admin_dashboard'))

    if role not in ('Admin', 'Reviewer'):
        flash("Invalid role specified.", "error")
        return redirect(url_for('admin_dashboard'))

    try:
        ws = get_worksheet(USERS_SHEET_NAME)
        headers = [h.strip().lower() for h in ws.row_values(1)]
        all_users = ws.get_all_records()
        if any(str(u.get('emailid', '')).strip().lower() == emailid for u in all_users):
            flash("Email already exists in the system.", "error")
            print("Duplicate email detected:", emailid)
            return redirect(url_for('admin_dashboard'))

        new_row_dict = {
            'name': name,
            'emailid': emailid,
            'role': role,
            'mobile': mobile,
            'designation': designation,
            'status': status,
            'password': password
        }
        new_row = [new_row_dict.get(h, "") for h in headers]
        print("Attempting to append row:", new_row)
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        print("Row appended successfully!")
        flash("Employee added successfully!", "success")
    except Exception as e:
        print("Append failed:", e)
        flash(f"Failed to add employee: {str(e)}", "error")
    return redirect(url_for('admin_dashboard'))

from flask import request, jsonify

# Update employee route
@app.route("/admin/update_employee", methods=["POST"])
@login_required(required_role="admin")
def update_employee():
    """
    Expected form data:
      - originalEmail : the existing email (lowercase)
      - name, emailid, role, status, mobile (optional), designation (optional)
    This function finds the row by matching 'emailid' column and updates the row in place.
    Returns JSON { success: True, employee: {...} } on success, or { success: False, error: '...' }.
    """
    original = request.form.get("originalEmail", "").strip().lower()
    name = request.form.get("name", "").strip()
    emailid = request.form.get("emailid", "").strip().lower()
    role = request.form.get("role", "").strip().capitalize()
    status = request.form.get("status", "Active").strip().capitalize()
    mobile = request.form.get("mobile", "").strip()
    designation = request.form.get("designation", "").strip()

    if not original:
        return jsonify(success=False, error="Missing original email to identify row"), 400

    try:
        ws = get_worksheet(USERS_SHEET_NAME)
        rows = ws.get_all_values()
        if not rows or len(rows) < 1:
            return jsonify(success=False, error="Sheet is empty or missing headers"), 500

        headers = [h.strip().lower() for h in rows[0]]
        try:
            idx_email = headers.index("emailid")
        except ValueError:
            return jsonify(success=False, error="Sheet missing 'emailid' header"), 500

        # find row index (1-based for gspread)
        target_row_idx = None
        for i, r in enumerate(rows[1:], start=2):
            cell_email = (r[idx_email].strip().lower() if idx_email < len(r) else "")
            if cell_email == original:
                target_row_idx = i
                break

        if target_row_idx is None:
            return jsonify(success=False, error="Original email not found in sheet"), 404

        # prepare updated row preserving header order
        update_dict = {
            'name': name,
            'emailid': emailid,
            'role': role,
            'mobile': mobile,
            'designation': designation,
            'status': status,
            # keep password unchanged here (do not overwrite unless provided)
        }

        # read current row values to preserve unspecified columns (like password)
        current_row = rows[target_row_idx - 1]  # zero-based index into rows list
        new_row = []
        for h in headers:
            new_row.append(update_dict.get(h, current_row[headers.index(h)] if headers.index(h) < len(current_row) else ""))

        # Update the entire row
        ws.update(f"A{target_row_idx}", [new_row], value_input_option="USER_ENTERED")

        updated_employee = {
            "name": name,
            "emailid": emailid,
            "role": role,
            "status": status,
            "mobile": mobile,
            "designation": designation
        }
        return jsonify(success=True, employee=updated_employee)
    except Exception as e:
        print("Update failed:", e)
        return jsonify(success=False, error=str(e)), 500

# ----------------- Entry -----------------
if __name__ == "__main__":
    app.run(host="localhost", port=5000, debug=True)
