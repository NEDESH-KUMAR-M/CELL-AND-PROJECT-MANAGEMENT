from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file, Response
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
import os
import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io
import requests
from functools import wraps
import uuid
import re
import csv
import backoff
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch # Optional: for 429 handling
from googleapiclient.errors import HttpError  # Optional: for 429 handling

# ---------------------- Config ----------------------
SERVICE_ACCOUNT_FILE = os.environ.get(
    "GSA_KEY_FILE", "synthetic-trail-467009-e8-876a545fffbd.json"
)
SHEET_ID = "1xV2S1xxiomM5Mj5EorzXUkWD0jhlBhFOvqqPxwJuiec"
USERS_SHEET_NAME = "users"
PROJECTS_SHEET_NAME = "projects"
DRIVE_FOLDER_ID = "1TY2NPru24W2UcNAuuLIPpQyEeMiy29Pi"
CELL_DRIVE_ID = "1VwLir9mbwuPCegMevbrXvlYKsFK4G8VM"
GOOGLE_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbx0DlarHT1HJRjf-xNMnn4-U4vp0kvBikXRigOSEIv4yrbOu_2xrgU1h2um_bkY5JFQug/exec"
CELL_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbyJNfTOYm5RD7VapSN9AGFwoSVdFPJ0-pzQTpIZggNZeUEoUI5IevxZm9gNFOV0SqAYJg/exec"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Enable CORS for all routes
CORS(app, resources={r"/*": {"origins": ["http://127.0.0.1:5000", "http://192.168.1.244:5000"]}})

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ----------------- Google Sheets helpers -----------------
def get_gspread_client():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError(f"Service account file not found: {SERVICE_ACCOUNT_FILE}")
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return gspread.authorize(creds)

def get_project_details(project_id):
    """Helper function to get project details from worksheet"""
    try:
        ws = get_worksheet(PROJECTS_SHEET_NAME)
        headers = [h.strip().lower() for h in ws.row_values(1)]
        row_values = ws.row_values(project_id)
        if not row_values:
            return None
        if len(row_values) < len(headers):
            row_values += [''] * (len(headers) - len(row_values))
        return dict(zip(headers, row_values))
    except Exception as e:
        print(f"Error getting project details: {e}")
        return None

def get_worksheet(sheet_name):
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        raise RuntimeError(f"Worksheet '{sheet_name}' not found in the spreadsheet")

def fetch_user_by_email(email):
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
        idx_name = headers.index("name")  # Add name index
    except ValueError:
        raise RuntimeError("Sheet must have headers: name | emailid | role | mobile | designation | status | password")
    for r in rows[1:]:
        if len(r) <= idx_email:
            continue
        if r[idx_email].strip().lower() == email.strip().lower():
            return {
                "emailid": r[idx_email].strip(),  # Use "emailid" to match sheet
                "password": r[idx_password].strip() if len(r) > idx_password else "",
                "role": r[idx_role].strip().lower() if len(r) > idx_role else "",
                "status": r[idx_status].strip().lower() if len(r) > idx_status else "",
                "name": r[idx_name].strip() if len(r) > idx_name else ""  # Add name
            }
    return None

def fetch_all_employees():
    ws = get_worksheet(USERS_SHEET_NAME)
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        print("No data or headers found in users sheet")
        return []
    headers = [h.strip().lower() for h in rows[0]]
    employees = []
    for r in rows[1:]:
        emp = {}
        for i, h in enumerate(headers):
            emp[h] = r[i].strip() if i < len(r) else ""
        employees.append(emp)
    print(f"Fetched {len(employees)} employees from sheet")
    return employees

def upload_image_to_drive(image_file, name, folder_id=DRIVE_FOLDER_ID):
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    drive_service = build('drive', 'v3', credentials=creds)
    file_extension = os.path.splitext(image_file.filename)[1]
    new_filename = f"{name}{file_extension}"
    file_stream = io.BytesIO(image_file.read())
    file_metadata = {'name': new_filename, 'parents': [folder_id]}
    media = MediaIoBaseUpload(file_stream, mimetype=image_file.mimetype, resumable=True)
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id', supportsAllDrives=True).execute()
    file_id = file.get('id')
    try:
        drive_service.permissions().create(fileId=file_id, body={'role': 'reader', 'type': 'anyone'}, supportsAllDrives=True).execute()
    except Exception as e:
        print(f"Warning: Could not set permissions for file {file_id}: {e}")
    return file_id

def get_project_sheet_worksheet(project_sheet_id, sheet_name, create_if_missing=False):
    """Get a worksheet from a project-specific Google Sheet"""
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(project_sheet_id)
        try:
            return sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            if not create_if_missing:
                return None
            ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=20)
            return ws
    except Exception as e:
        print(f"Error accessing project sheet {project_sheet_id}: {e}")
        return None

# Headers used for the cell_information sheet
CELL_INFO_HEADERS = ['id', 'name', 'layoutType', 'status', 'layouters', 'createdAt', 'subsheet_name', 'cellimage', 'reviewer', 'reviewdate', 'completionPercentage']

def initialize_cell_information_sheet(project_sheet_id):
    """Initialize the cell_information worksheet in the project sheet"""
    try:
        ws = get_project_sheet_worksheet(project_sheet_id, "cell_information", create_if_missing=True)
        if not ws:
            return None
        # Ensure headers exist and are correct
        existing_values = ws.get_all_values()
        if not existing_values:
            ws.append_row(CELL_INFO_HEADERS, value_input_option='USER_ENTERED')
        else:
            first_row = [h.strip() for h in (existing_values[0] if existing_values else [])]
            if first_row != CELL_INFO_HEADERS:
                # Overwrite first row with correct headers
                ws.update('A1', [CELL_INFO_HEADERS], value_input_option='USER_ENTERED')
        return ws
    except Exception as e:
        print(f"Error initializing cell_information sheet: {e}")
        return None

def get_project_sheet_id(project_id):
    """Return the per-project Google Sheet ID from the master `projects` worksheet by row index."""
    try:
        ws = get_worksheet(PROJECTS_SHEET_NAME)
        headers = [h.strip().lower() for h in ws.row_values(1)]
        row_values = ws.row_values(project_id)
        if not row_values:
            return None
        if len(row_values) < len(headers):
            row_values += [''] * (len(headers) - len(row_values))
        record = dict(zip(headers, row_values))
        return record.get('sheetid', '')
    except Exception as e:
        print(f"Error getting project sheet ID: {e}")
        return None

# Utility: set dropdown validations for checklist subsheet
def set_checklist_dropdowns(project_sheet_id: str, subsheet_title: str, max_rows: int = 1000) -> None:
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        sheets_service = build('sheets', 'v4', credentials=creds)
        gc = get_gspread_client()
        sh = gc.open_by_key(project_sheet_id)
        ws = sh.worksheet(subsheet_title)
        sheet_id = ws.id
        requests_body = {
            "requests": [
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": max_rows,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [
                                    {"userEnteredValue": "yes"},
                                    {"userEnteredValue": "no"},
                                    {"userEnteredValue": "not-applicable"},
                                    {"userEnteredValue": "YES"},
                                    {"userEnteredValue": "NO"},
                                    {"userEnteredValue": "NOT APPLICABLE"}
                                ]
                            },
                            "showCustomUi": True,
                            "strict": True
                        }
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": max_rows,
                            "startColumnIndex": 3,
                            "endColumnIndex": 4
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [
                                    {"userEnteredValue": "completed"},
                                    {"userEnteredValue": "action-needed"},
                                    {"userEnteredValue": "not-reviewed"},
                                    {"userEnteredValue": "rev&completed"},
                                    {"userEnteredValue": "rev&action needed"},
                                    {"userEnteredValue": "not reviewed"}
                                ]
                            },
                            "showCustomUi": True,
                            "strict": True
                        }
                    }
                }
            ]
        }
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=project_sheet_id,
            body=requests_body
        ).execute()
    except Exception as e:
        print(f"Warning: Failed to set dropdowns on subsheet '{subsheet_title}': {e}")

def get_checklist_items(layoutType):
    layoutType = layoutType.lower().strip()  # Normalize layoutType
    print(f"Fetching checklist items for layoutType: '{layoutType}'")  # Debug log
    if layoutType == 'cell':
        return [
            {"id": 1, "description": "Is the cell layout area discussed and finalized including the width and height?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 2, "description": "Is the main power/ground metal track width sufficient?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 3, "description": "Is the internal power/ground routing verified?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 4, "description": "Were the critical signal lists discussed with designer and shieldings implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 5, "description": "In XT018, did we implement double/triple DTI's sufficiently?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 6, "description": "Did the metal orientations discussed and implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 7, "description": "Are the schematics well annotated regarding current density? Were the metal widths used sufficient for rated currents?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 8, "description": "Were the signal routing widths, clearance between wires depending on voltage potentials discussed with designer and implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 9, "description": "Did we implement transistor matching schemes for current mirrors, differential pairs?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 10, "description": "Did we implement resistor matching schemes where needed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 11, "description": "Were the dummies added for matching structures of transistors and resistors if needed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 12, "description": "In XT018, did we consider DIFF density issue with large resistor placements?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 13, "description": "Were the device names matched and made visible in layout according to the names given in schematic for future references?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 14, "description": "Were Antenna errors taken into account for MIM caps if used?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 15, "description": "Were popping errors considered if larger metal planes are used?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 16, "description": "For larger devices, is the gate connections impedance calculated and verified with designer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 17, "description": "Has the layout been done such that it will not be flipped 90 degrees at the next level up? Common device orientation helps maintain uniform process variations.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 18, "description": "Has large devices been broken up into smaller units. This makes it less susceptible to process gradients and improves matching.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 19, "description": "Has all instructions in schematic annotation been adhered to?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 20, "description": "Are pins brought out to the appropriate edges of the cell, with the labels facing back into the cell? Adjust the origin of the label appropriately.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 21, "description": "Have all gates been connected through metal, rather than through poly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 22, "description": "Have all metal option requirements been provided for?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 23, "description": "Is minimum 2 via placement rule followed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 24, "description": "Is the voltage rating of signals considered while routing?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 25, "description": "Will this layout be placed multiple times on next level? If so, is the abutment of each sides considered?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 26, "description": "If there is any capacitor's multipliers/dimension changes, was it discussed with designer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 27, "description": "When using an opamp, make sure that the output is routed in metal to the feedback circuit. If not, take into account the resistance of the poly routing track or connect the following stage to the feedback circuit instead of the output of the opamp.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 28, "description": "When using big pmos transistors, make sure that the n-well is contacted regularly. Due to the big capacitance between drain and bulk (n-well) the n-well is pulled below source voltage when the pmos is switched off. This leads to unexpected high current consumption or dynamic latchup. Worst case it leads to complete latchup or malfunction of neighboring function blocks.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 29, "description": "If pmos transistors (poly to n-well capacitor) are used as capacitor, take into account that the bulk (n-well) has a high series resistance. This resistance can be reduced by increased number of well contacts.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 30, "description": "Make sure that matched Poly, 2 resistors have either all metal on top or none has metal on top.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 31, "description": "Were DRC checks without warnings/errors done?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 32, "description": "Were LVS checks without warnings/errors done?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""}
        ]
    elif layoutType == 'iolayout':
        return [
            {"id": 1, "description": "Is the chip size discussed and finalized before IO placement?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 2, "description": "Is the pad size discussed and finalized before IO placement?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 3, "description": "Were the power/ground routing for IO pads considered?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 4, "description": "Were each IO's ESD placement discussed with designer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 5, "description": "Are there any custom pads? If so, pad openings/slots/planarity considered?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 6, "description": "Are all pins labelled on the top metal label layer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 7, "description": "Were the pin/pad order discussed with designer and customer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 8, "description": "Were the bond out scheme and packaging analyzed before finalizing the pad locations?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 9, "description": "Were DRC checks without warnings/errors done?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 10, "description": "Were LVS checks without warnings/errors done?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""}
        ]
    elif layoutType == 'tapelayout':
        return [
            {"id": 1, "description": "Is the Module Selection finalized? If yes, specify the relevant document path.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 2, "description": "Do we have a clean DRC results including special checks?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 3, "description": "Is the DRC directory zipped and copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 4, "description": "Do we have a clean LVS results including floating gate & floating well checks?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 5, "description": "Is the LVS directory zipped and copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 6, "description": "Is layout review done and all points have positive comments?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 7, "description": "Is the Pre-tapeout summary document updated in svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 8, "description": "Is the Metal option list document copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 9, "description": "Is the Pin Coordinates document copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 10, "description": "Is final layout GDS stream-out done and verified of any warning/errors?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 11, "description": "Is the GDS zipped and copied to svn along with xstreamout summary and log files?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 12, "description": "Was the latest PVS - DRC,LVS runset used?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 13, "description": "SiFo:- Any export control applicable?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 14, "description": "SiFo:- Is the Mask set defined?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 15, "description": "SiFo:- Any special scribe lane requirements?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 16, "description": "SiFo:- Is the sawplan & wafermap approved?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 17, "description": "SiFo:- Do we have any wafers in 'Hold position'?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 18, "description": "SiFo:- Backgrinding required?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 19, "description": "SiFo:- Design layers inputted?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 20, "description": "SiFo:- Design Information inputted?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 21, "description": "Is the GDS tar file copied to xfab server?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 22, "description": "Was the above mentioned SiFo information has been shared with customer and approval has been received?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 23, "description": "Is final SiFo in SVN?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 24, "description": "Is the database read only?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 25, "description": "Is the database tared and concatenated with date and time?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 26, "description": "Is the final netlists stored in SVN?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 27, "description": "Is the final schematic and layout tree copied to SVN?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 28, "description": "Is schematic dumped in pdf?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 29, "description": "Is the project option copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 30, "description": "Is the Layout report prepared and copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""}
        ]
    elif layoutType == 'toplayout':
        return [
            {"id": 1, "description": "Are all the sub blocks reviewed and finalized?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 2, "description": "Are all the sub blocks available in main library? Are all instances instantiated in the top layout referred to main library?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 3, "description": "Have blocks been placed as close as practicable possible to their power and ground pins?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 4, "description": "Are blocks close to the pin that they have to connect to?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 5, "description": "Have you optimised block placement to ensure that the blocks with the most connections to each other are close to each other?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 6, "description": "Has about 20% extra space been allowed for routing and shielding?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 7, "description": "Are all the power/ground track widths maintained the same from pad to sub blocks?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 8, "description": "Some nets need to be short. Has care been taken to identify these nets and implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 9, "description": "Has appropriate action been taken to minimise parasitics, (short interconnect, high metals, intermediate metals)?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 10, "description": "Has a power/ground plan been created early when floorplanning?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 11, "description": "Have you discussed with the designer how wide the main power rails need to be?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 12, "description": "If a power or ground needs to be clean, has it been star connected back to the pad?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 13, "description": "Is there a separate track for the substrate connection?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 14, "description": "Are all the signal routings done around the sub blocks?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 15, "description": "Were the signal routing widths, clearance between wires depending on voltage potentials discussed with designer and implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 16, "description": "Tub potentials need to be clearly defined. Ask the designer if not sure.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 17, "description": "Uncommitted substrate must be well defined.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 18, "description": "Has care been taken to avoid parasitic field transistors (METAL1 => max 50V / METAL2 => max 70V across active)?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 19, "description": "All handle wafer diodes are connected through, to the correct potential (it has happened that LVS failed to flag a hard short of the HW connections!). Use a manual highlight of the HW net (anode and cathode) to make sure they are all connected through.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 20, "description": "Are sensitive signals shielded and were the shielding nets (GND/VDD) approved by designer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 21, "description": "Are the lengthy routed digital signals buffered?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 22, "description": "Are there any sensitive analog blocks under bondpads?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 23, "description": "Is the final layout size rounded to 1um?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 24, "description": "Were adding of blockage layer discussed with designer and implemented?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 25, "description": "If you redesign a layout with a partial masksset, then make sure to run a 'mask-compare' check with the original tape-out source on CVS/PFUS. (ex. Metal1 redesign)", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 26, "description": "Check the placement of sensitive and matching circuit parts according to mechanical stress. Don’t place these circuits near the chip border. (e.g. bandgap, oscillator, sensors, etc.)", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 27, "description": "Are the correct starpoints foreseen in layout (floorplanning!)? Supply/ground of noisy vs. sensitive lines, connection to power driver supply/ground lines, Ensure you don’t have supply ring in star point concept!", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 28, "description": "Are analog and digital supply and ground lines separated?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 29, "description": "All bondpad connections need to be screened for their current capability, towards the connected nets. This is to be manually checked, if possible through current density simulation (Magwell or R3D). This needs to be proven pre-tapeout in the pin list document.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 30, "description": "Were the DRC checks without warnings/errors completed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 31, "description": "Were special DRC checks like density, popping, antenna, latch-up, triple dti for dzbti performed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 32, "description": "Were LVS check without warnings/ERC errors completed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 33, "description": "Was the GDS exported and imported to verify DRC/LVS again?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""}
        ]
    elif layoutType == 'customlayout':
        return []  # Custom layout starts with empty checklist
    else:
        print(f"Unknown layoutType: '{layoutType}', returning empty checklist")  # Debug log
        return []  # Default empty for unknown types

def create_checklist(subsheet_ws, layoutType):
    checklist_headers = ['id', 'description', 'applicable', 'status', 'comment']
    print(f"Creating checklist for layoutType: '{layoutType}' in subsheet: {subsheet_ws.title}")  # Debug log
    
    # Clear existing content and set headers
    try:
        subsheet_ws.clear()  # Clear all content in the subsheet
        subsheet_ws.update(range_name='A1', values=[checklist_headers], value_input_option='USER_ENTERED')
        print(f"Headers set: {checklist_headers}")  # Debug log
    except Exception as e:
        print(f"Error setting headers in subsheet {subsheet_ws.title}: {e}")
        raise
    
    # Get and append checklist items
    checklist_items = get_checklist_items(layoutType.lower().strip())
    print(f"Retrieved {len(checklist_items)} checklist items for layoutType: '{layoutType}'")  # Debug log
    
    if checklist_items:
        for item in checklist_items:
            try:
                item_row = [item.get(h, '') for h in checklist_headers]
                subsheet_ws.append_row(item_row, value_input_option='USER_ENTERED')
                print(f"Appended checklist item: {item_row}")  # Debug log
            except Exception as e:
                print(f"Error appending checklist item {item.get('id')}: {e}")
                raise
    else:
        print(f"No checklist items to append for layoutType: '{layoutType}'")
# ----------------- Auth decorators -----------------
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
            print("User data:", user)  # Debug
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
        session["user"] = {
            "email": user["emailid"],  # Use "emailid" to match sheet
            "role": user["role"],
            "name": user["name"] or "Unknown"  # Use "name" with fallback
        }
        print("Session user:", session["user"])  # Debug
        session.permanent = False
        if user["role"] == "admin":
            return redirect(url_for("projects_page"))
        else:
            return redirect(url_for("employee_dashboard"))
    return render_template("login.html")

@app.route('/api/image/<string:file_id>')
@login_required()  # Allow all logged-in users to view images
def get_drive_image(file_id):
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        drive_service = build('drive', 'v3', credentials=creds)
        file_metadata = drive_service.files().get(fileId=file_id, fields='mimeType', supportsAllDrives=True).execute()
        mimetype = file_metadata.get('mimeType', 'application/octet-stream')
        request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        file_stream.seek(0)
        return send_file(file_stream, mimetype=mimetype, as_attachment=False)
    except Exception as e:
        print(f"Error fetching image {file_id}: {e}")
        return jsonify(success=False, error=f"Image not found: {str(e)}"), 404

@app.route("/home")
@login_required()
def home():
    u = session["user"]
    return f"Welcome {u['email']}! Your role is {u['role']}."

@app.route("/admin")
@login_required(required_role="admin")
def admin_dashboard():
    employees = fetch_all_employees()
    user = session.get("user", {})
    admin_info = next((e for e in employees if e.get("emailid", "").lower() == user.get("email", "").lower()), None)
    return render_template("admin_dashboard.html", user=admin_info)

@app.route("/employee")
@login_required(required_role="reviewer")
def employee_dashboard():
    return render_template("empdash.html")

@app.route("/open_project/<int:project_id>")
@login_required()
def open_project(project_id):
    try:
        ws = get_worksheet(PROJECTS_SHEET_NAME)
        headers = [h.strip().lower() for h in ws.row_values(1)]
        row_values = ws.row_values(project_id)
        
        if not row_values:
            flash("Project not found", 'error')
            return redirect(url_for('projects_page'))
            
        if len(row_values) < len(headers):
            row_values += [''] * (len(headers) - len(row_values))
            
        record = dict(zip(headers, row_values))
        project = {
            'id': project_id,
            'clientName': record.get('clientname', ''),
            'projectName': record.get('projectname', ''),
            'description': record.get('description', ''),
            'version': record.get('version', ''),
            'status': record.get('status', ''),
            'reviewers': [r.strip() for r in record.get('reviewers', '').split(',') if r.strip()],
            'technologies': [t.strip() for t in record.get('technologies', '').split(',') if t.strip()],
            'projectimage': record.get('projectimage', ''),
            'createdate': record.get('createdate', ''),
            'enddate': record.get('enddate', ''),
            'sheetid': record.get('sheetid', ''),
            'folderid': record.get('folderid', '')
        }
        
        return render_template("openproject.html", project=project)
    except Exception as e:
        print(f"Error opening project: {str(e)}")  # Log the error
        flash(f"Error opening project: {str(e)}", 'error')
        return redirect(url_for('projects_page'))
@app.route("/employee/open_project/<int:project_id>")
@login_required()
def employee_open_project(project_id):
    try:
        ws = get_worksheet(PROJECTS_SHEET_NAME)
        headers = [h.strip().lower() for h in ws.row_values(1)]
        row_values = ws.row_values(project_id)
        
        if not row_values:
            flash("Project not found", 'error')
            return redirect(url_for('projects_page'))
            
        if len(row_values) < len(headers):
            row_values += [''] * (len(headers) - len(row_values))
            
        record = dict(zip(headers, row_values))
        project = {
            'id': project_id,
            'clientName': record.get('clientname', ''),
            'projectName': record.get('projectname', ''),
            'description': record.get('description', ''),
            'version': record.get('version', ''),
            'status': record.get('status', ''),
            'reviewers': [r.strip() for r in record.get('reviewers', '').split(',') if r.strip()],
            'technologies': [t.strip() for t in record.get('technologies', '').split(',') if t.strip()],
            'projectimage': record.get('projectimage', ''),
            'createdate': record.get('createdate', ''),
            'enddate': record.get('enddate', ''),
            'sheetid': record.get('sheetid', ''),
            'folderid': record.get('folderid', '')
        }
        
        return render_template("empopenprojects.html", project=project)
    except Exception as e:
        print(f"Error opening project: {str(e)}")  # Log the error
        flash(f"Error opening project: {str(e)}", 'error')
        return redirect(url_for('projects_page'))

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))

@app.route("/admin/add_employee", methods=["POST"])
@login_required(required_role="admin")
def add_employee():
    name = request.form.get("name", "").strip()
    emailid = request.form.get("emailid", "").strip().lower()
    role = request.form.get("role", "").strip().capitalize()
    password = request.form.get("password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()
    mobile = request.form.get("mobile", "").strip()
    designation = request.form.get("designation", "").strip()
    status = request.form.get("status", "Active").strip().capitalize()

    if not all([name, emailid, role, password, confirm_password]):
        return jsonify(success=False, error="All required fields must be filled."), 400

    if password != confirm_password:
        return jsonify(success=False, error="Passwords do not match."), 400

    if role not in ('Admin', 'Reviewer'):
        return jsonify(success=False, error="Invalid role specified."), 400

    try:
        ws = get_worksheet(USERS_SHEET_NAME)
        headers = [h.strip().lower() for h in ws.row_values(1)]
        all_users = ws.get_all_records()
        if any(str(u.get('emailid', '')).strip().lower() == emailid for u in all_users):
            print("Duplicate email detected:", emailid)
            return jsonify(success=False, error="Email already exists in the system."), 400

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
        return jsonify(success=True, message="Employee added successfully!")
    except Exception as e:
        print("Append failed:", e)
        return jsonify(success=False, error=str(e)), 500

@app.route("/admin/update_employee", methods=["POST"])
@login_required(required_role="admin")
def update_employee():
    original = request.form.get("originalEmail", "").strip().lower()
    name = request.form.get("name", "").strip()
    emailid = request.form.get("emailid", "").strip().lower()
    role = request.form.get("role", "").strip().capitalize()
    password = request.form.get("password", "").strip()
    status = request.form.get("status", "Active").strip().capitalize()

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
            idx_name = headers.index("name")
            idx_role = headers.index("role")
            idx_status = headers.index("status")
            idx_password = headers.index("password")
        except ValueError as e:
            return jsonify(success=False, error=f"Sheet missing required header: {str(e)}"), 500

        target_row_idx = None
        for i, r in enumerate(rows[1:], start=2):
            cell_email = r[idx_email].strip().lower() if idx_email < len(r) else ""
            if cell_email == original:
                target_row_idx = i
                break

        if target_row_idx is None:
            return jsonify(success=False, error="Original email not found in sheet"), 404

        current_row = rows[target_row_idx - 1].copy()

        if name:
            current_row[idx_name] = name
        if emailid and emailid != original:
            all_users = ws.get_all_records()
            if any(str(u.get('emailid', '')).strip().lower() == emailid for u in all_users):
                return jsonify(success=False, error="Email already exists in the system"), 400
            current_row[idx_email] = emailid
        if role:
            current_row[idx_role] = role
        if status:
            current_row[idx_status] = status
        if password:
            current_row[idx_password] = password

        ws.update(f"A{target_row_idx}", [current_row], value_input_option="USER_ENTERED")
        print(f"Updated employee {original} at row {target_row_idx}")

        updated_employee = dict(zip(headers, current_row))
        return jsonify(success=True, employee=updated_employee)

    except Exception as e:
        print(f"Update failed: {e}")
        return jsonify(success=False, error=str(e)), 500

@app.route("/projects")
@login_required()  # Allow all logged-in users to access projects page
def projects_page():
    return render_template("projects.html")

@app.route("/api/projects", methods=["GET"])
@login_required()  # Allow all logged-in users to view projects
def api_get_projects():
    try:
        ws = get_worksheet(PROJECTS_SHEET_NAME)
        records = ws.get_all_records()
        processed_records = []
        for i, record in enumerate(records):
            processed_record = record.copy()
            processed_record['id'] = i + 2
            processed_record['reviewers'] = [r.strip() for r in record.get('reviewers', '').split(',') if r.strip()]
            processed_record['technologies'] = [t.strip() for t in record.get('technologies', '').split(',') if t.strip()]
            file_id = record.get('projectimage', '')
            processed_record['projectimage'] = file_id
            processed_records.append(processed_record)
        return jsonify(processed_records)
    except Exception as e:
        print(f"Error fetching projects: {e}")
        return jsonify(error=str(e)), 500

@app.route("/api/projects", methods=["POST"])
@login_required(required_role="admin")
def api_add_project():
    try:
        ws = get_worksheet(PROJECTS_SHEET_NAME)
        headers = ws.row_values(1)
        project_name = request.form.get('projectName', 'Untitled Project').strip()
        image_id = ''
        if 'projectImage' in request.files:
            image_file = request.files['projectImage']
            if image_file.filename != '':
                image_id = upload_image_to_drive(image_file, project_name)
        new_row_dict = {
            'clientname': request.form.get('clientName', '').strip(),
            'projectname': project_name,
            'description': request.form.get('description', '').strip(),
            'version': request.form.get('version', '').strip(),
            'status': request.form.get('status', 'active').replace('-', ' ').title(),
            'reviewers': request.form.get('reviewers', '').strip(),
            'technologies': request.form.get('technologies', '').strip(),
            'projectimage': image_id,
            'createdate': datetime.datetime.now().strftime("%d/%m/%Y"),
            'enddate': ''
        }
        num_data_rows = len(ws.get_all_records())
        new_project_id = num_data_rows + 2
        new_row_list = [new_row_dict.get(h, '') for h in headers]
        ws.append_row(new_row_list, value_input_option="USER_ENTERED")
        
        # Trigger the Google Apps Script after adding the project
        trigger_response = requests.post(GOOGLE_SCRIPT_URL, json={
            "projectName": project_name,
            "projectRow": new_project_id
        })
        
        if trigger_response.status_code != 200:
            print(f"Warning: Google Apps Script trigger failed with status {trigger_response.status_code}: {trigger_response.text}")
        
        response_project = new_row_dict.copy()
        response_project['id'] = new_project_id
        response_project['reviewers'] = [r.strip() for r in new_row_dict.get('reviewers', '').split(',') if r.strip()]
        response_project['technologies'] = [t.strip() for t in new_row_dict.get('technologies', '').split(',') if t.strip()]
        return jsonify(success=True, message="Project added successfully!", project=response_project), 201
    except Exception as e:
        print(f"Error adding project: {e}")
        return jsonify(success=False, error=str(e)), 500

@app.route("/api/projects/<int:project_id>", methods=["PUT"])
@login_required(required_role="admin")
def api_update_project(project_id):
    try:
        ws = get_worksheet(PROJECTS_SHEET_NAME)
        headers = ws.row_values(1)
        current_values = ws.row_values(project_id)
        current_data_dict = dict(zip(headers, current_values))
        project_name = request.form.get('projectName', 'Untitled Project').strip()
        if 'projectImage' in request.files:
            image_file = request.files['projectImage']
            if image_file.filename != '':
                current_data_dict['projectimage'] = upload_image_to_drive(image_file, project_name)
        old_status = current_data_dict.get('status', '').lower().replace(' ', '-')
        new_status = request.form.get('status', '').lower()
        updated_form_data = {
            'clientname': request.form.get('clientName', '').strip(),
            'projectname': project_name,
            'description': request.form.get('description', '').strip(),
            'version': request.form.get('version', '').strip(),
            'status': request.form.get('status', 'active').replace('-', ' ').title(),
            'reviewers': request.form.get('reviewers', '').strip(),
            'technologies': request.form.get('technologies', '').strip(),
        }
        current_data_dict.update(updated_form_data)
        if new_status == 'completed' and old_status != 'completed':
            current_data_dict['enddate'] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        updated_row_list = [current_data_dict.get(h, '') for h in headers]
        ws.update(f'A{project_id}', [updated_row_list], value_input_option="USER_ENTERED")
        response_project = current_data_dict.copy()
        response_project['id'] = project_id
        response_project['reviewers'] = [r.strip() for r in current_data_dict.get('reviewers', '').split(',') if r.strip()]
        response_project['technologies'] = [t.strip() for t in current_data_dict.get('technologies', '').split(',') if t.strip()]
        return jsonify(success=True, message="Project updated successfully!", project=response_project)
    except Exception as e:
        print(f"Error updating project {project_id}: {e}")
        return jsonify(success=False, error=str(e)), 500

@app.route("/api/employees", methods=["GET"])
@login_required(required_role="admin")
def api_get_employees():
    try:
        employees = fetch_all_employees()
        if not employees:
            print("No employees found in the sheet")
        return jsonify(employees)
    except Exception as e:
        print(f"Error fetching employees: {e}")
        return jsonify(success=False, error=str(e)), 500
@app.route("/api/profile", methods=["GET"])
@login_required(required_role=None)  # Allow any logged-in user
def api_get_profile():
    try:
        user_email = session['user']['email']
        user = fetch_user_by_email(user_email)
        if not user:
            print(f"User not found for email: {user_email}")
            return jsonify({"success": False, "error": "User not found"}), 404
        return jsonify({
            "name": user['name'],
            "role": user['role'],
            "status": user['status'],
            "emailid": user['emailid'],
            "mobile": user.get('mobile', ''),
            "designation": user.get('designation', '')
        })
    except Exception as e:
        print(f"Error fetching profile: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/profile/password", methods=["PUT"])
@login_required(required_role=None)
def api_update_password():
    try:
        data = request.get_json()
        new_password = data.get('password')
        if not new_password or len(new_password) < 8:
            return jsonify({"success": False, "error": "Password must be at least 8 characters"}), 400
        
        user_email = session['user']['email']
        ws = get_worksheet(USERS_SHEET_NAME)
        rows = ws.get_all_values()
        headers = [h.strip().lower() for h in rows[0]]
        
        try:
            idx_email = headers.index("emailid")
            idx_password = headers.index("password")
        except ValueError:
            return jsonify({"success": False, "error": "Sheet missing required headers"}), 500

        target_row_idx = None
        for i, r in enumerate(rows[1:], start=2):
            if len(r) > idx_email and r[idx_email].strip().lower() == user_email.lower():
                target_row_idx = i
                break
        
        if target_row_idx is None:
            print(f"User not found for email: {user_email}")
            return jsonify({"success": False, "error": "User not found"}), 404

        ws.update_cell(target_row_idx, idx_password + 1, new_password)
        print(f"Password updated for user: {user_email}")
        return jsonify({"success": True, "message": "Password updated successfully"})
    except Exception as e:
        print(f"Error updating password: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
@app.route("/profile")
@login_required(required_role=None)
def profile_page():
    return render_template("profile.html")
@app.route("/eprofile")
@login_required(required_role=None)
def eprofile_page():
    return render_template("eprofile.html")

@app.route('/api/projects/<int:project_id>', methods=['GET'])
@login_required()
def api_get_project(project_id):
    try:
        ws = get_worksheet(PROJECTS_SHEET_NAME)
        headers = [h.strip().lower() for h in ws.row_values(1)]
        row_values = ws.row_values(project_id)
        if not row_values:
            return jsonify(error='Project not found'), 404
        if len(row_values) < len(headers):
            row_values += [''] * (len(headers) - len(row_values))
        record = dict(zip(headers, row_values))
        resp = {
            'id': project_id,
            'clientName': record.get('clientname', ''),
            'projectName': record.get('projectname', ''),
            'description': record.get('description', ''),
            'version': record.get('version', ''),
            'status': record.get('status', ''),
            'reviewers': [r.strip() for r in record.get('reviewers', '').split(',') if r.strip()],
            'technologies': [t.strip() for t in record.get('technologies', '').split(',') if t.strip()],
            'projectimage': record.get('projectimage', ''),
            'createdate': record.get('createdate', ''),
            'enddate': record.get('enddate', ''),
            'sheetid': record.get('sheetid', ''),
            'folderid': record.get('folderid', '')
        }
        return jsonify(resp)
    except Exception as e:
        print(f"Error fetching project {project_id}: {e}")
        return jsonify(error=str(e)), 500

@app.route('/api/cells/<string:cell_id>', methods=['GET'])
@login_required()
def api_get_cell(cell_id):
    project_id = request.args.get('project_id')
    if not project_id:
        return jsonify(error='project_id required'), 400
    
    try:
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        ws = get_project_sheet_worksheet(project_sheet_id, "cell_information")
        if not ws:
            return jsonify(error='cell_information sheet not found'), 404
        
        records = ws.get_all_records()
        for r in records:
            if str(r.get('id', '')) == cell_id:
                return jsonify({
                    'id': cell_id,
                    'name': r.get('name', ''),
                    'layoutType': r.get('layoutType', 'custom'),
                    'status': r.get('status', 'not-started'),
                    'layouters': [x.strip() for x in r.get('layouters', '').split(',') if x.strip()],
                    'createdAt': r.get('createdAt', ''),
                    'subsheet_name': r.get('subsheet_name', ''),
                    'cellimage': r.get('cellimage', ''),
                    'reviewer': r.get('reviewer', ''),
                    'reviewdate': r.get('reviewdate', '')
                })
        return jsonify(error='Cell not found'), 404
    except Exception as e:
        print(f"Error fetching cell {cell_id}: {e}")
        return jsonify(error=str(e)), 500

# --- Modified Cells CRUD: stored in project-specific sheet ---
@app.route('/api/cells', methods=['GET'])
@login_required()
def api_get_cells():
    project_id = request.args.get('project_id') or request.args.get('id')
    if not project_id:
        return jsonify(error='project_id query param required'), 400
    
    try:
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        ws = get_project_sheet_worksheet(project_sheet_id, "cell_information")
        if not ws:
            return jsonify([])
        
        records = ws.get_all_records()
        out = []
        for i, r in enumerate(records, start=2):
            if i == 1:  # Skip header row
                continue
            out.append({
                'id': str(r.get('id', i)),
                'name': r.get('name') or '',
                'layoutType': r.get('layouttype') or r.get('layoutType') or 'custom',
                'status': r.get('status') or 'not-started',
                'layouters': [x.strip() for x in (r.get('layouters') or '').split(',') if x.strip()],
                'createdAt': r.get('createdat') or r.get('createdate') or '',
                'subsheet_name': r.get('subsheet_name') or '',
                'cellimage': r.get('cellimage') or '',
                'reviewer': r.get('reviewer') or '',
                'reviewdate': r.get('reviewdate') or '',
                'completionPercentage': r.get('completionPercentage', 0)  # New: defaults to 0
            })
        return jsonify(out)
    except Exception as e:
        print(f"Error fetching cells for project {project_id}: {e}")
        return jsonify(error=str(e)), 500

@app.route('/api/cells', methods=['POST'])
@login_required()
def api_add_cell():
    project_id = request.args.get('project_id') or request.form.get('project_id') or request.json.get('project_id')
    if not project_id:
        return jsonify(error='project_id required'), 400
    
    try:
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        ws = initialize_cell_information_sheet(project_sheet_id)
        if not ws:
            return jsonify(error='Failed to access cell_information sheet'), 500
        
        headers = [h.strip() for h in ws.row_values(1)]
        data = request.json if request.is_json else request.form.to_dict()
        
        name = data.get('name')
        layoutType = data.get('layoutType', 'Custom').lower().strip()  # Normalize layoutType
        # Map input layoutType values to standardized spellings
        layoutType_map = {
            'io': 'IO',
            'iolayout': 'IO',
            'cell': 'Cell',
            'top': 'Top',
            'toplayout': 'Top',
            'tapeout': 'Tape Out',
            'tapelayout': 'Tape Out',
            'custom': 'Custom',
            'customlayout': 'Custom'
        }
        mapped_layoutType = layoutType_map.get(layoutType, 'Custom')  # Default to 'Custom' if unknown
        
        status = data.get('status')
        layouters = data.get('layouters')
        
        print(f"Adding cell with original layoutType: '{layoutType}', mapped to: '{mapped_layoutType}' for project_id: {project_id}")  # Debug log
        
        if isinstance(layouters, list):
            layouters = ','.join([str(l) for l in layouters])
        
        createdAt = data.get('createdAt', datetime.datetime.now().isoformat())
        cell_id = str(uuid.uuid4())
        
        subsheet_name = re.sub(r'[^a-zA-Z0-9\s]', '', name).replace(' ', '_')[:30]
        
        row = {
            'id': cell_id,
            'name': name or '',
            'layoutType': mapped_layoutType,  # Store mapped layoutType in cell_information
            'status': status or 'not-started',
            'layouters': layouters or '',
            'createdAt': createdAt,
            'subsheet_name': subsheet_name,
            'cellimage': '',
            'reviewer': '',
            'reviewdate': ''
        }
        
        row_list = [row.get(h, '') for h in headers]
        ws.append_row(row_list, value_input_option='USER_ENTERED')
        print(f"Cell added to cell_information: {row_list}")  # Debug log
        
        subsheet_ws = get_project_sheet_worksheet(project_sheet_id, subsheet_name, create_if_missing=True)
        if not subsheet_ws:
            return jsonify(error='Failed to create or access subsheet'), 500
        
        create_checklist(subsheet_ws, mapped_layoutType)  # Pass mapped layoutType
        set_checklist_dropdowns(project_sheet_id, subsheet_name)
        print(f"Checklist created and dropdowns set for subsheet: {subsheet_name}")  # Debug log
        
        # Optional trigger
        project_details = get_project_details(project_id)
        if project_details:
            trigger_data = {
                "projectName": project_details.get('projectname', ''),
                "subSheetName": subsheet_name,
                "layoutType": mapped_layoutType,  # Use mapped layoutType
                "projectSheetId": project_sheet_id
            }
            try:
                response = requests.post(CELL_SCRIPT_URL, json=trigger_data)
                if response.status_code != 200:
                    print(f"Warning: Subsheet creation trigger failed: {response.text}")
            except Exception as e:
                print(f"Error triggering subsheet creation: {e}")
        
        response_cell = row.copy()
        response_cell['layouters'] = [x.strip() for x in (row.get('layouters') or '').split(',') if x.strip()]
        return jsonify(success=True, message='Cell added', cell=response_cell), 201
    
    except Exception as e:
        print(f"Error adding cell for project {project_id}: {e}")
        return jsonify(error=str(e)), 500
# ---------------------- Modify Cell API Route ----------------------
# Handles updating or deleting a cell within a project's cell_information sheet.
# For updates, allows modification of cell details and renaming of associated subsheet.
# Accessible to all logged-in users.
@app.route('/api/cells/<string:cell_id>', methods=['PUT', 'DELETE'])
@login_required()
def api_modify_cell(cell_id):
    project_id = request.args.get('project_id') or request.form.get('project_id') or request.json.get('project_id')
    if not project_id:
        return jsonify(error='project_id required'), 400
    
    try:
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        ws = get_project_sheet_worksheet(project_sheet_id, "cell_information")
        if not ws:
            return jsonify(error='cell_information sheet not found'), 404
        
        headers = [h.strip() for h in ws.row_values(1)]
        idx_id = headers.index('id') if 'id' in headers else -1
        
        if idx_id == -1:
            return jsonify(error='Sheet missing id column'), 500
        
        rows = ws.get_all_values()
        target_row_idx = None
        
        for i, r in enumerate(rows[1:], start=2):  # Skip header row
            if len(r) > idx_id and r[idx_id] == cell_id:
                target_row_idx = i
                break
        
        if target_row_idx is None:
            return jsonify(error='Cell not found'), 404
        
        if request.method == 'DELETE':
            # Deletion logic omitted as per request
            pass
        
        # PUT -> update
        data = request.json if request.is_json else request.form.to_dict()
        current = ws.row_values(target_row_idx)
        
        if len(current) < len(headers):
            current += [''] * (len(headers) - len(current))
        
        row_map = dict(zip(headers, current))
        
        old_name = row_map['name']
        old_subsheet = row_map['subsheet_name']
        
        for k in ['name', 'layoutType', 'status', 'layouters', 'reviewer', 'reviewdate', 'completionPercentage']:
            if k in data:
                val = data[k]
                if k == 'layouters' and isinstance(val, list):
                    val = ','.join([str(v) for v in val])
                row_map[k] = val
        
        if 'name' in data and data['name'] != old_name:
            new_name = data['name']
            new_subsheet = re.sub(r'[^a-zA-Z0-9\s]', '', new_name).replace(' ', '_')[:30]
            gc = get_gspread_client()
            sh = gc.open_by_key(project_sheet_id)
            try:
                subsheet = sh.worksheet(old_subsheet)
                subsheet.update_title(new_subsheet)
                row_map['subsheet_name'] = new_subsheet
            except gspread.exceptions.WorksheetNotFound:
                print(f"Warning: Subsheet {old_subsheet} not found, skipping rename")
        
        new_row = [row_map.get(h, '') for h in headers]
        ws.update(f'A{target_row_idx}', [new_row], value_input_option="USER_ENTERED")
        
        response_cell = row_map.copy()
        response_cell['layouters'] = [x.strip() for x in (row_map.get('layouters') or '').split(',') if x.strip()]
        return jsonify(success=True, message='Cell updated', cell=response_cell)
    
    except Exception as e:
        print(f"Error modifying cell {cell_id} for project {project_id}: {e}")
        return jsonify(error=str(e)), 500
@app.route('/api/cells/<string:cell_id>/image', methods=['POST'])
@login_required()
def api_upload_cell_image(cell_id):
    project_id = request.args.get('project_id')
    if not project_id:
        return jsonify(error='project_id required'), 400
    if 'cellImage' not in request.files:
        return jsonify(error='No cellImage file'), 400
    image_file = request.files['cellImage']
    if image_file.filename == '':
        return jsonify(error='No selected file'), 400
    
    try:
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        ws = get_project_sheet_worksheet(project_sheet_id, "cell_information")
        if not ws:
            return jsonify(error='cell_information sheet not found'), 404
        
        rows = ws.get_all_values()
        headers = [h.strip() for h in rows[0]]
        idx_id = headers.index('id')
        idx_name = headers.index('name')
        idx_cellimage = headers.index('cellimage')
        target_row = None
        for i, r in enumerate(rows[1:], 2):
            if len(r) > idx_id and r[idx_id] == cell_id:
                target_row = i
                break
        if not target_row:
            return jsonify(error='Cell not found'), 404
        
        cell_name = rows[target_row-1][idx_name]
        file_id = upload_image_to_drive(image_file, cell_name, folder_id=CELL_DRIVE_ID)
        ws.update_cell(target_row, idx_cellimage + 1, file_id)
        return jsonify(success=True, cellimage=file_id)
    except Exception as e:
        print(f"Error uploading cell image: {e}")
        return jsonify(error=str(e)), 500

# New endpoint to get checklist data from a specific cell's subsheet
def get_checklist_items(layoutType):
    layoutType = layoutType.lower().strip()  # Normalize layoutType
    # Map legacy/shorthand layoutType values to expected values
    layoutType_map = {
        'io': 'iolayout',
        'tapeout': 'tapelayout',
        'top': 'toplayout',
        'cell': 'cell',
        'customlayout': 'customlayout'
    }
    mapped_layoutType = layoutType_map.get(layoutType, layoutType)
    print(f"Fetching checklist items for original layoutType: '{layoutType}', mapped to: '{mapped_layoutType}'")  # Debug log

    if mapped_layoutType == 'cell':
        return [
            {"id": 1, "description": "Is the cell layout area discussed and finalized including the width and height?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 2, "description": "Is the main power/ground metal track width sufficient?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 3, "description": "Is the internal power/ground routing verified?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 4, "description": "Were the critical signal lists discussed with designer and shieldings implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 5, "description": "In XT018, did we implement double/triple DTI's sufficiently?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 6, "description": "Did the metal orientations discussed and implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 7, "description": "Are the schematics well annotated regarding current density? Were the metal widths used sufficient for rated currents?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 8, "description": "Were the signal routing widths, clearance between wires depending on voltage potentials discussed with designer and implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 9, "description": "Did we implement transistor matching schemes for current mirrors, differential pairs?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 10, "description": "Did we implement resistor matching schemes where needed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 11, "description": "Were the dummies added for matching structures of transistors and resistors if needed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 12, "description": "In XT018, did we consider DIFF density issue with large resistor placements?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 13, "description": "Were the device names matched and made visible in layout according to the names given in schematic for future references?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 14, "description": "Were Antenna errors taken into account for MIM caps if used?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 15, "description": "Were popping errors considered if larger metal planes are used?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 16, "description": "For larger devices, is the gate connections impedance calculated and verified with designer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 17, "description": "Has the layout been done such that it will not be flipped 90 degrees at the next level up? Common device orientation helps maintain uniform process variations.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 18, "description": "Has large devices been broken up into smaller units. This makes it less susceptible to process gradients and improves matching.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 19, "description": "Has all instructions in schematic annotation been adhered to?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 20, "description": "Are pins brought out to the appropriate edges of the cell, with the labels facing back into the cell? Adjust the origin of the label appropriately.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 21, "description": "Have all gates been connected through metal, rather than through poly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 22, "description": "Have all metal option requirements been provided for?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 23, "description": "Is minimum 2 via placement rule followed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 24, "description": "Is the voltage rating of signals considered while routing?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 25, "description": "Will this layout be placed multiple times on next level? If so, is the abutment of each sides considered?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 26, "description": "If there is any capacitor's multipliers/dimension changes, was it discussed with designer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 27, "description": "When using an opamp, make sure that the output is routed in metal to the feedback circuit. If not, take into account the resistance of the poly routing track or connect the following stage to the feedback circuit instead of the output of the opamp.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 28, "description": "When using big pmos transistors, make sure that the n-well is contacted regularly. Due to the big capacitance between drain and bulk (n-well) the n-well is pulled below source voltage when the pmos is switched off. This leads to unexpected high current consumption or dynamic latchup. Worst case it leads to complete latchup or malfunction of neighboring function blocks.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 29, "description": "If pmos transistors (poly to n-well capacitor) are used as capacitor, take into account that the bulk (n-well) has a high series resistance. This resistance can be reduced by increased number of well contacts.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 30, "description": "Make sure that matched Poly, 2 resistors have either all metal on top or none has metal on top.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 31, "description": "Were DRC checks without warnings/errors done?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 32, "description": "Were LVS checks without warnings/errors done?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""}
        ]
    elif mapped_layoutType == 'iolayout':
        return [
            {"id": 1, "description": "Is the chip size discussed and finalized before IO placement?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 2, "description": "Is the pad size discussed and finalized before IO placement?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 3, "description": "Were the power/ground routing for IO pads considered?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 4, "description": "Were each IO's ESD placement discussed with designer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 5, "description": "Are there any custom pads? If so, pad openings/slots/planarity considered?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 6, "description": "Are all pins labelled on the top metal label layer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 7, "description": "Were the pin/pad order discussed with designer and customer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 8, "description": "Were the bond out scheme and packaging analyzed before finalizing the pad locations?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 9, "description": "Were DRC checks without warnings/errors done?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 10, "description": "Were LVS checks without warnings/errors done?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""}
        ]
    elif mapped_layoutType == 'tapelayout':
        return [
            {"id": 1, "description": "Is the Module Selection finalized? If yes, specify the relevant document path.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 2, "description": "Do we have a clean DRC results including special checks?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 3, "description": "Is the DRC directory zipped and copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 4, "description": "Do we have a clean LVS results including floating gate & floating well checks?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 5, "description": "Is the LVS directory zipped and copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 6, "description": "Is layout review done and all points have positive comments?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 7, "description": "Is the Pre-tapeout summary document updated in svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 8, "description": "Is the Metal option list document copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 9, "description": "Is the Pin Coordinates document copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 10, "description": "Is final layout GDS stream-out done and verified of any warning/errors?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 11, "description": "Is the GDS zipped and copied to svn along with xstreamout summary and log files?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 12, "description": "Was the latest PVS - DRC,LVS runset used?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 13, "description": "SiFo:- Any export control applicable?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 14, "description": "SiFo:- Is the Mask set defined?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 15, "description": "SiFo:- Any special scribe lane requirements?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 16, "description": "SiFo:- Is the sawplan & wafermap approved?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 17, "description": "SiFo:- Do we have any wafers in 'Hold position'?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 18, "description": "SiFo:- Backgrinding required?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 19, "description": "SiFo:- Design layers inputted?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 20, "description": "SiFo:- Design Information inputted?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 21, "description": "Is the GDS tar file copied to xfab server?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 22, "description": "Was the above mentioned SiFo information has been shared with customer and approval has been received?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 23, "description": "Is final SiFo in SVN?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 24, "description": "Is the database read only?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 25, "description": "Is the database tared and concatenated with date and time?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 26, "description": "Is the final netlists stored in SVN?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 27, "description": "Is the final schematic and layout tree copied to SVN?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 28, "description": "Is schematic dumped in pdf?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 29, "description": "Is the project option copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 30, "description": "Is the Layout report prepared and copied to svn?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""}
        ]
    elif mapped_layoutType == 'toplayout':
        return [
            {"id": 1, "description": "Are all the sub blocks reviewed and finalized?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 2, "description": "Are all the sub blocks available in main library? Are all instances instantiated in the top layout referred to main library?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 3, "description": "Have blocks been placed as close as practicable possible to their power and ground pins?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 4, "description": "Are blocks close to the pin that they have to connect to?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 5, "description": "Have you optimised block placement to ensure that the blocks with the most connections to each other are close to each other?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 6, "description": "Has about 20% extra space been allowed for routing and shielding?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 7, "description": "Are all the power/ground track widths maintained the same from pad to sub blocks?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 8, "description": "Some nets need to be short. Has care been taken to identify these nets and implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 9, "description": "Has appropriate action been taken to minimise parasitics, (short interconnect, high metals, intermediate metals)?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 10, "description": "Has a power/ground plan been created early when floorplanning?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 11, "description": "Have you discussed with the designer how wide the main power rails need to be?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 12, "description": "If a power or ground needs to be clean, has it been star connected back to the pad?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 13, "description": "Is there a separate track for the substrate connection?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 14, "description": "Are all the signal routings done around the sub blocks?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 15, "description": "Were the signal routing widths, clearance between wires depending on voltage potentials discussed with designer and implemented accordingly?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 16, "description": "Tub potentials need to be clearly defined. Ask the designer if not sure.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 17, "description": "Uncommitted substrate must be well defined.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 18, "description": "Has care been taken to avoid parasitic field transistors (METAL1 => max 50V / METAL2 => max 70V across active)?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 19, "description": "All handle wafer diodes are connected through, to the correct potential (it has happened that LVS failed to flag a hard short of the HW connections!). Use a manual highlight of the HW net (anode and cathode) to make sure they are all connected through.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 20, "description": "Are sensitive signals shielded and were the shielding nets (GND/VDD) approved by designer?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 21, "description": "Are the lengthy routed digital signals buffered?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 22, "description": "Are there any sensitive analog blocks under bondpads?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 23, "description": "Is the final layout size rounded to 1um?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 24, "description": "Were adding of blockage layer discussed with designer and implemented?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 25, "description": "If you redesign a layout with a partial masksset, then make sure to run a 'mask-compare' check with the original tape-out source on CVS/PFUS. (ex. Metal1 redesign)", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 26, "description": "Check the placement of sensitive and matching circuit parts according to mechanical stress. Don’t place these circuits near the chip border. (e.g. bandgap, oscillator, sensors, etc.)", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 27, "description": "Are the correct starpoints foreseen in layout (floorplanning!)? Supply/ground of noisy vs. sensitive lines, connection to power driver supply/ground lines, Ensure you don’t have supply ring in star point concept!", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 28, "description": "Are analog and digital supply and ground lines separated?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 29, "description": "All bondpad connections need to be screened for their current capability, towards the connected nets. This is to be manually checked, if possible through current density simulation (Magwell or R3D). This needs to be proven pre-tapeout in the pin list document.", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 30, "description": "Were the DRC checks without warnings/errors completed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 31, "description": "Were special DRC checks like density, popping, antenna, latch-up, triple dti for dzbti performed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 32, "description": "Were LVS check without warnings/ERC errors completed?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""},
            {"id": 33, "description": "Was the GDS exported and imported to verify DRC/LVS again?", "applicable": "not-applicable", "status": "not-reviewed", "comment": ""}
        ]
    elif mapped_layoutType == 'customlayout':
        return []  # Custom layout starts with empty checklist
    else:
        print(f"Unknown layoutType after mapping: '{mapped_layoutType}', returning empty checklist")  # Debug log
        return []  # Default empty for unknown types
# Add this function after the existing api_get_cells route or in the checklist section
@app.route('/api/checklist', methods=['GET'])
@login_required()
def api_get_checklist():
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    
    if not project_id or not cell_id:
        return jsonify(error='project_id and cell_id required'), 400
    
    try:
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        cell_info_ws = get_project_sheet_worksheet(project_sheet_id, "cell_information")
        if not cell_info_ws:
            return jsonify(error='cell_information sheet not found'), 404
        
        cells = cell_info_ws.get_all_records()
        subsheet_name = None
        layoutType = None
        for cell in cells:
            if str(cell.get('id', '')) == cell_id:
                subsheet_name = cell.get('subsheet_name')
                layoutType = cell.get('layoutType', 'customlayout')
                break
        
        if not subsheet_name:
            return jsonify(error='Cell not found'), 404
        
        checklist_ws = get_project_sheet_worksheet(project_sheet_id, subsheet_name)
        if not checklist_ws:
            return jsonify(error='Checklist sheet not found'), 404
        
        records = checklist_ws.get_all_records()
        checklist_items = []
        headers = ['id', 'description', 'applicable', 'status', 'comment']
        
        for record in records:
            item = {h: record.get(h, '') for h in headers}
            item['id'] = str(record.get('id', ''))  # Ensure ID is string for consistency
            checklist_items.append(item)
        
        # If no items exist, initialize with default items based on layoutType
        if not checklist_items:
            create_checklist(checklist_ws, layoutType)
            set_checklist_dropdowns(project_sheet_id, subsheet_name)
            records = checklist_ws.get_all_records()
            for record in records:
                item = {h: record.get(h, '') for h in headers}
                item['id'] = str(record.get('id', ''))  # Ensure ID is string
                checklist_items.append(item)
        
        return jsonify(checklist_items)
    
    except Exception as e:
        print(f"Error fetching checklist for cell {cell_id} in project {project_id}: {e}")
        return jsonify(error=str(e)), 500
# New endpoint to update checklist item
@app.route('/api/checklist/<string:item_id>', methods=['PUT'])
@login_required()
def api_update_checklist_item(item_id):
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    
    if not project_id or not cell_id:
        return jsonify(error='project_id and cell_id required'), 400
    
    try:
        data = request.json
        if not data:
            return jsonify(error='No data provided for update'), 400
        
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        cell_info_ws = get_project_sheet_worksheet(project_sheet_id, "cell_information")
        if not cell_info_ws:
            return jsonify(error='cell_information sheet not found'), 404
        
        cells = cell_info_ws.get_all_records()
        subsheet_name = None
        
        for cell in cells:
            if str(cell.get('id', '')) == cell_id:
                subsheet_name = cell.get('subsheet_name')
                break
        
        if not subsheet_name:
            return jsonify(error='Cell not found'), 404
        
        checklist_ws = get_project_sheet_worksheet(project_sheet_id, subsheet_name)
        if not checklist_ws:
            return jsonify(error='Checklist sheet not found'), 404
        
        headers = [h.strip() for h in checklist_ws.row_values(1)]
        rows = checklist_ws.get_all_values()
        target_row_idx = None
        
        for i, r in enumerate(rows[1:], start=2):  # Skip header row
            if len(r) > 0 and str(r[0]) == item_id:
                target_row_idx = i
                break
        
        if target_row_idx is None:
            return jsonify(error='Checklist item not found'), 404
        
        current_row = rows[target_row_idx - 1].copy()
        if len(current_row) < len(headers):
            current_row += [''] * (len(headers) - len(current_row))
        
        row_map = dict(zip(headers, current_row))
        
        for field in ['description', 'applicable', 'status', 'comment']:
            if field in data:
                row_map[field] = data[field]
        
        new_row = [row_map.get(h, '') for h in headers]
        
        checklist_ws.update(f'A{target_row_idx}', [new_row], value_input_option='USER_ENTERED')
        
        return jsonify(success=True, message='Checklist item updated', item=row_map)
    
    except Exception as e:
        print(f"Error updating checklist item: {e}")
        return jsonify(error=str(e)), 500

# New endpoint to add checklist item to a cell
@app.route('/api/checklist', methods=['POST'])
@login_required()
def api_add_checklist_item():
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    
    if not project_id or not cell_id:
        return jsonify(error='project_id and cell_id required'), 400
    
    try:
        data = request.json
        if not data or 'description' not in data:
            return jsonify(error='Description is required'), 400
        
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        cell_info_ws = get_project_sheet_worksheet(project_sheet_id, "cell_information")
        if not cell_info_ws:
            return jsonify(error='cell_information sheet not found'), 404
        
        cells = cell_info_ws.get_all_records()
        subsheet_name = None
        layoutType = None
        
        for cell in cells:
            if str(cell.get('id', '')) == cell_id:
                subsheet_name = cell.get('subsheet_name')
                layoutType = cell.get('layoutType', 'custom')
                break
        
        if not subsheet_name:
            return jsonify(error='Cell not found'), 404
        
        checklist_ws = get_project_sheet_worksheet(project_sheet_id, subsheet_name)
        if not checklist_ws:
            return jsonify(error='Checklist sheet not found'), 404
        
        existing_items = checklist_ws.get_all_records()
        new_id = max([item.get('id', 0) for item in existing_items], default=0) + 1
        
        new_item = {
            'id': new_id,
            'description': data.get('description', ''),
            'applicable': data.get('applicable', 'not-applicable'),
            'status': data.get('status', 'not-reviewed'),
            'comment': data.get('comment', '')
        }
        
        headers = [h.strip() for h in checklist_ws.row_values(1)]
        new_row = [new_item.get(h, '') for h in headers]
        checklist_ws.append_row(new_row, value_input_option='USER_ENTERED')
        
        # Re-apply dropdowns after adding new row
        set_checklist_dropdowns(project_sheet_id, subsheet_name)
        
        return jsonify(success=True, message='Checklist item added', item=new_item), 201
    
    except Exception as e:
        print(f"Error adding checklist item: {e}")
        return jsonify(error=str(e)), 500


# New endpoint to delete checklist item
@app.route('/api/checklist/<string:item_id>', methods=['DELETE'])
@login_required()
def api_delete_checklist_item(item_id):
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    
    if not project_id or not cell_id:
        return jsonify(error='project_id and cell_id required'), 400
    
    try:
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify(error='Project sheet not found'), 404
        
        cell_info_ws = get_project_sheet_worksheet(project_sheet_id, "cell_information")
        if not cell_info_ws:
            return jsonify(error='cell_information sheet not found'), 404
        
        cells = cell_info_ws.get_all_records()
        subsheet_name = None
        
        for cell in cells:
            if str(cell.get('id', '')) == cell_id:
                subsheet_name = cell.get('subsheet_name')
                break
        
        if not subsheet_name:
            return jsonify(error='Cell not found'), 404
        
        checklist_ws = get_project_sheet_worksheet(project_sheet_id, subsheet_name)
        if not checklist_ws:
            return jsonify(error='Checklist sheet not found'), 404
        
        headers = [h.strip() for h in checklist_ws.row_values(1)]
        rows = checklist_ws.get_all_values()
        target_row_idx = None
        
        for i, r in enumerate(rows[1:], start=2):  # Skip header row
            if len(r) > 0 and str(r[0]) == item_id:
                target_row_idx = i
                break
        
        if target_row_idx is None:
            return jsonify(error='Checklist item not found'), 404
        
        checklist_ws.delete_rows(target_row_idx)
        
        return jsonify(success=True, message='Checklist item deleted')
    
    except Exception as e:
        print(f"Error deleting checklist item: {e}")
        return jsonify(error=str(e)), 500

@app.route('/celllayout')
@login_required()
def celllayout_page():
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    if not project_id or not cell_id:
        flash('Missing project_id or cell_id', 'error')
        return redirect(url_for('projects_page'))
    try:
        project = get_project_details(int(project_id))
        if not project:
            flash('Project not found', 'error')
            return redirect(url_for('projects_page'))
        project_ctx = {
            'id': int(project_id),
            'projectname': project.get('projectname', ''),
            'clientname': project.get('clientname', ''),
            'status': project.get('status', ''),
            'version': project.get('version', ''),
        }
        return render_template('celllayout.html', project=project_ctx, cell_id=cell_id)
    except Exception as e:
        print(f"Error rendering celllayout page: {e}")
        flash('Failed to open cell layout page', 'error')
        return redirect(url_for('projects_page'))

@app.route('/iolayout')
@login_required()
def iolayout_page():
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    if not project_id or not cell_id:
        flash('Missing project_id or cell_id', 'error')
        return redirect(url_for('projects_page'))
    try:
        project = get_project_details(int(project_id))
        if not project:
            flash('Project not found', 'error')
            return redirect(url_for('projects_page'))
        project_ctx = {
            'id': int(project_id),
            'projectname': project.get('projectname', ''),
            'clientname': project.get('clientname', ''),
            'status': project.get('status', ''),
            'version': project.get('version', ''),
        }
        return render_template('iolayout.html', project=project_ctx, cell_id=cell_id)
    except Exception as e:
        print(f"Error rendering iolayout page: {e}")
        flash('Failed to open IO layout page', 'error')
        return redirect(url_for('projects_page'))

@app.route('/customlayout')
@login_required()
def customlayout_page():
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    if not project_id or not cell_id:
        flash('Missing project_id or cell_id', 'error')
        return redirect(url_for('projects_page'))
    try:
        project = get_project_details(int(project_id))
        if not project:
            flash('Project not found', 'error')
            return redirect(url_for('projects_page'))
        project_ctx = {
            'id': int(project_id),
            'projectname': project.get('projectname', ''),
            'clientname': project.get('clientname', ''),
            'status': project.get('status', ''),
            'version': project.get('version', ''),
        }
        return render_template('customlayout.html', project=project_ctx, cell_id=cell_id)
    except Exception as e:
        print(f"Error rendering customlayout page: {e}")
        flash('Failed to open custom layout page', 'error')
        return redirect(url_for('projects_page'))

@app.route('/tapelayout')
@login_required()
def tapelayout_page():
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    if not project_id or not cell_id:
        flash('Missing project_id or cell_id', 'error')
        return redirect(url_for('projects_page'))
    try:
        project = get_project_details(int(project_id))
        if not project:
            flash('Project not found', 'error')
            return redirect(url_for('projects_page'))
        project_ctx = {
            'id': int(project_id),
            'projectname': project.get('projectname', ''),
            'clientname': project.get('clientname', ''),
            'status': project.get('status', ''),
            'version': project.get('version', ''),
        }
        return render_template('tapelayout.html', project=project_ctx, cell_id=cell_id)
    except Exception as e:
        print(f"Error rendering tapelayout page: {e}")
        flash('Failed to open tapeout layout page', 'error')
        return redirect(url_for('projects_page'))

@app.route('/toplayout')
@login_required()
def toplayout_page():
    project_id = request.args.get('project_id')
    cell_id = request.args.get('cell_id')
    if not project_id or not cell_id:
        flash('Missing project_id or cell_id', 'error')
        return redirect(url_for('projects_page'))
    try:
        project = get_project_details(int(project_id))
        if not project:
            flash('Project not found', 'error')
            return redirect(url_for('projects_page'))
        project_ctx = {
            'id': int(project_id),
            'projectname': project.get('projectname', ''),
            'clientname': project.get('clientname', ''),
            'status': project.get('status', ''),
            'version': project.get('version', ''),
        }
        return render_template('toplayout.html', project=project_ctx, cell_id=cell_id)
    except Exception as e:
        print(f"Error rendering toplayout page: {e}")
        flash('Failed to open top layout page', 'error')
        return redirect(url_for('projects_page'))

@app.route('/open_project/cell.html')
@login_required()
def open_project_cell_redirect():
    cell_id = request.args.get('id')
    project_id = request.args.get('project_id')
    if not project_id or not cell_id:
        flash('Missing project_id or cell_id', 'error')
        return redirect(url_for('projects_page'))
    # Redirect to the correct cell layout route
    return redirect(url_for('celllayout_page', project_id=project_id, cell_id=cell_id))
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch, mm
from reportlab.lib.pagesizes import A4
from flask import jsonify, request, Response
import io
import datetime
import os

@app.route('/api/export/pdf')
@login_required()
def export_pdf():
    project_id = request.args.get('project_id')
    if not project_id:
        return jsonify({'error': 'Project ID is required'}), 400

    try:
        # Fetch project details from the projects sheet
        ws = get_worksheet('projects')
        headers = [h.strip().lower() for h in ws.row_values(1)]
        row_values = ws.row_values(int(project_id))
        if not row_values:
            return jsonify({'error': 'Project not found'}), 404
        if len(row_values) < len(headers):
            row_values += [''] * (len(headers) - len(row_values))
        project = dict(zip(headers, row_values))

        # Fetch cell data from the project-specific sheet
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify({'error': 'Project sheet not found'}), 404
        
        cell_ws = get_project_sheet_worksheet(project_sheet_id, 'cell_information')
        if not cell_ws:
            return jsonify({'error': 'cell_information sheet not found'}), 404
        
        cells = cell_ws.get_all_records()
        total_cells = len(cells)
        completed_cells = len([c for c in cells if c.get('status', '').lower() == 'completed'])
        in_progress_cells = len([c for c in cells if c.get('status', '').lower() == 'in progress'])
        not_started_cells = len([c for c in cells if c.get('status', '').lower() == 'not started'])
        progress = round((completed_cells / total_cells * 100) if total_cells > 0 else 0)

        # Create PDF buffer
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, 
            pagesize=A4,
            rightMargin=20*mm, 
            leftMargin=20*mm, 
            topMargin=20*mm, 
            bottomMargin=20*mm
        )
        
        # Custom professional styles
        styles = getSampleStyleSheet()
        
        # Title style - Big and bold for cover page
        title_style = ParagraphStyle(
            'Title',
            parent=styles['Heading1'],
            fontSize=28,
            spaceAfter=20,
            textColor=colors.HexColor('#2C3E50'),
            alignment=1,  # Center aligned
            fontName='Helvetica-Bold'
        )
        
        # Project name style for cover page
        project_name_style = ParagraphStyle(
            'ProjectName',
            parent=styles['Heading2'],
            fontSize=22,
            spaceAfter=20,
            textColor=colors.HexColor('#34495E'),
            alignment=1,  # Center aligned
            fontName='Helvetica-Bold'
        )
        
        # Company name style for cover page
        company_name_style = ParagraphStyle(
            'CompanyName',
            parent=styles['Normal'],
            fontSize=16,
            spaceAfter=20,
            textColor=colors.HexColor('#34495E'),
            alignment=1,  # Center aligned
            fontName='Helvetica-Bold'
        )
        
        # Subtitle style
        subtitle_style = ParagraphStyle(
            'Subtitle',
            parent=styles['Heading2'],
            fontSize=16,
            spaceAfter=12,
            textColor=colors.HexColor('#34495E'),
            fontName='Helvetica-Bold'
        )
        
        # Normal style
        normal_style = ParagraphStyle(
            'Normal',
            parent=styles['Normal'],
            fontSize=10,
            spaceAfter=6,
            textColor=colors.HexColor('#2C3E50'),
            fontName='Helvetica'
        )
        
        # Small style for footer
        footer_style = ParagraphStyle(
            'Footer',
            parent=styles['Normal'],
            fontSize=8,
            spaceAfter=0,
            textColor=colors.HexColor('#7F8C8D'),
            fontName='Helvetica',
            alignment=0  # Left aligned
        )
        
        # Table header style
        table_header_style = ParagraphStyle(
            'TableHeader',
            parent=styles['Normal'],
            fontSize=10,
            textColor=colors.white,
            alignment=1,
            fontName='Helvetica-Bold'
        )
        
        elements = []
        
        # Cover page - Reordered: logo, company name, project report, project name
        elements.append(Spacer(1, 1*inch))
        # Add company logo with fixed container size
        logo_path = os.path.join('static', 'image', 'Logo.png')
        if os.path.exists(logo_path):
            logo = Image(logo_path, width=100*mm, height=50*mm)
            logo.hAlign = 'CENTER'
            elements.append(logo)
            elements.append(Spacer(1, 0.3*inch))
        elements.append(Paragraph('Epical Layouts Private Limited', company_name_style))
        elements.append(Spacer(1, 0.3*inch))
        elements.append(Paragraph('PROJECT REPORT', title_style))
        elements.append(Spacer(1, 0.3*inch))
        elements.append(Paragraph(project.get("projectname", "Unknown Project"), project_name_style))
        elements.append(PageBreak())
        
        # Executive Summary and Overall Progress section (combined on one page)
        elements.append(Paragraph('EXECUTIVE SUMMARY', subtitle_style))
        elements.append(Spacer(1, 0.2*inch))
        
        # Key metrics table
        metrics_data = [
            ['Project Name', project.get("projectname", "N/A")],
            ['Client', project.get("clientname", "N/A")],
            ['Start Date', project.get("createdate", "N/A")],
            ['Current Status', project.get("status", "N/A")],
            ['Version', project.get("version", "N/A")],
            ['Total Cells', str(total_cells)]
        ]
        
        metrics_table = Table(metrics_data, colWidths=[doc.width/3*1, doc.width/3*2])
        metrics_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F8F9F9')),
            ('LINEABOVE', (0, 0), (-1, 0), 1, colors.HexColor('#2C3E50')),
            ('LINEBELOW', (0, -1), (-1, -1), 1, colors.HexColor('#2C3E50')),
            ('LINEBEFORE', (0, 0), (0, -1), 1, colors.HexColor('#EAECEE')),
            ('LINEAFTER', (-1, 0), (-1, -1), 1, colors.HexColor('#EAECEE')),
        ]))
        elements.append(metrics_table)
        elements.append(Spacer(1, 0.3*inch))
        
        # Progress visualization
        elements.append(Paragraph('OVERALL PROGRESS', subtitle_style))
        elements.append(Spacer(1, 0.2*inch))
        
        # Simple progress text
        elements.append(Paragraph(f'Completion: {progress}%', normal_style))
        elements.append(Spacer(1, 0.2*inch))
        
        # Status distribution table
        if total_cells > 0:
            status_data = [
                ['Status', 'Count', 'Percentage'],
                ['Completed', str(completed_cells), f'{round(completed_cells/total_cells*100)}%'],
                ['In Progress', str(in_progress_cells), f'{round(in_progress_cells/total_cells*100)}%'],
                ['Not Started', str(not_started_cells), f'{round(not_started_cells/total_cells*100)}%']
            ]
            
            status_table = Table(status_data, colWidths=[doc.width*0.4, doc.width*0.3, doc.width*0.3])
            status_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2C3E50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D0D3D4')),
                ('BACKGROUND', (0, 1), (0, 1), colors.HexColor('#E8F5E9')),
                ('BACKGROUND', (0, 2), (0, 2), colors.HexColor('#FFF8E1')),
                ('BACKGROUND', (0, 3), (0, 3), colors.HexColor('#FFEBEE')),
            ]))
            elements.append(status_table)
        
        elements.append(PageBreak())
        
        # Cell details section - REVISED PAGINATION APPROACH
        if cells:
            elements.append(Paragraph('CELL DETAILS', subtitle_style))
            elements.append(Spacer(1, 0.2*inch))
            
            # Prepare table data with serial number and layouter
            table_data = []
            # Add header
            table_data.append([
                'S.No',
                'Cell Name',
                'Type',
                'Status',
                'Layouter',
                'Reviewer',
                'Review Date',
                'Progress'
            ])
            
            for idx, cell in enumerate(cells, start=1):
                status = str(cell.get('status', '')).lower()
                status_text = status.replace('-', ' ').title()
                cell_progress = cell.get('completionPercentage', 0)
                if not isinstance(cell_progress, (int, float)):
                    try:
                        cell_progress = float(cell_progress)
                    except (ValueError, TypeError):
                        cell_progress = 0
                
                table_data.append([
                    str(idx),
                    cell.get('name', ''),
                    str(cell.get('layoutType', 'custom')).capitalize(),
                    status_text,
                    cell.get('layouters', 'N/A'),
                    cell.get('reviewer', 'N/A'),
                    cell.get('reviewdate', 'N/A'),
                    f"{cell_progress}%"
                ])
            
            # Create the table with adjusted column widths
            col_widths = [
                doc.width * 0.08,  # S.No
                doc.width * 0.22,  # Name
                doc.width * 0.12,  # Type
                doc.width * 0.12,  # Status
                doc.width * 0.15,  # Layouter
                doc.width * 0.15,  # Reviewer
                doc.width * 0.16,  # Review Date
                doc.width * 0.10   # Progress
            ]
            
            # Use repeatRows=1 to ensure header appears on every page
            table = Table(table_data, colWidths=col_widths, repeatRows=1)
            
            # Table style
            table_style = TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2C3E50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D0D3D4')),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (0, 0), (0, -1), 'CENTER'),  # S.No centered
                ('ALIGN', (7, 0), (7, -1), 'CENTER'),  # Progress centered
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ])
            
            # Add row colors based on status
            start_row = 1
            for i in range(start_row, len(table_data)):
                status = str(table_data[i][3]).lower()
                if 'complete' in status:
                    table_style.add('BACKGROUND', (0, i), (-1, i), colors.HexColor('#E8F5E9'))
                elif 'progress' in status:
                    table_style.add('BACKGROUND', (0, i), (-1, i), colors.HexColor('#FFF8E1'))
                elif 'not' in status and 'start' in status:
                    table_style.add('BACKGROUND', (0, i), (-1, i), colors.HexColor('#FFEBEE'))
            
            # Add alternating row colors for rows without status-based coloring
            for i in range(start_row, len(table_data)):
                status = str(table_data[i][3]).lower()
                if i % 2 == 0 and not any([
                    'complete' in status,
                    'progress' in status,
                    'not' in status and 'start' in status
                ]):
                    table_style.add('BACKGROUND', (0, i), (-1, i), colors.HexColor('#F8F9F9'))
            
            table.setStyle(table_style)
            elements.append(table)
        else:
            elements.append(Paragraph('No cell data available for this project.', normal_style))
        
        # Build PDF with footer on every page
        doc.build(elements, onFirstPage=lambda canvas, doc: add_footer(canvas, doc, project),
                 onLaterPages=lambda canvas, doc: add_footer(canvas, doc, project))
        
        buffer.seek(0)
        
        # Create filename without special characters
        project_name = project.get("projectname", project_id)
        safe_filename = "".join(c for c in project_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
        safe_filename = safe_filename.replace(' ', '_') + '_Report.pdf'
        
        return Response(
            buffer, 
            mimetype='application/pdf', 
            headers={
                'Content-Disposition': f'attachment;filename={safe_filename}'
            }
        )
    except Exception as e:
        print(f"Error generating PDF for project {project_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

def add_footer(canvas, doc, project):
    """
    Add footer with generation date, company name, and copyright
    """
    canvas.saveState()
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(colors.HexColor('#7F8C8D'))
    
    # Add "Generated on" text at bottom left
    generated_text = f"Generated on {datetime.datetime.now().strftime('%Y-%m-%d ')}"
    canvas.drawString(20*mm, 15*mm, generated_text)
    
    # Add company name and copyright at bottom center
    company_text = ""
    canvas.drawCentredString(doc.pagesize[0]/2, 15*mm, company_text)
    
    canvas.restoreState()
@app.route('/api/export/csv')
@login_required()
def export_csv():
    project_id = request.args.get('project_id')
    if not project_id:
        return jsonify({'error': 'Project ID is required'}), 400

    try:
        # Fetch project details from the projects sheet
        ws = get_worksheet('projects')
        headers = [h.strip().lower() for h in ws.row_values(1)]
        row_values = ws.row_values(int(project_id))
        if not row_values:
            return jsonify({'error': 'Project not found'}), 404
        if len(row_values) < len(headers):
            row_values += [''] * (len(headers) - len(row_values))
        project = dict(zip(headers, row_values))

        # Fetch cell data from the project-specific sheet
        project_sheet_id = get_project_sheet_id(project_id)
        if not project_sheet_id:
            return jsonify({'error': 'Project sheet not found'}), 404
        
        cell_ws = get_project_sheet_worksheet(project_sheet_id, 'cell_information')
        if not cell_ws:
            return jsonify({'error': 'cell_information sheet not found'}), 404
        
        cells = cell_ws.get_all_records()
        total_cells = len(cells)
        completed_cells = len([c for c in cells if c.get('status', '').lower() == 'completed'])
        progress = round((completed_cells / total_cells * 100) if total_cells > 0 else 0)

        # Create CSV buffer
        buffer = io.StringIO()
        buffer.write('\ufeff')  # Add UTF-8 BOM for Excel compatibility
        writer = csv.writer(buffer, lineterminator='\n')

        # Write Project Details
        writer.writerow([f'Project Report: {project.get("projectname", "Unknown")}'])
        details = [
            f'Project Name: {project.get("projectname", "N/A")}',
            f'Client Name: {project.get("clientname", "N/A")}',
            f'Created Date: {project.get("createdate", "N/A")}',
            f'Status: {project.get("status", "N/A")}',
            f'Version: {project.get("version", "N/A")}',
            f'Progress: {progress}% ({completed_cells}/{total_cells} cells completed)'
        ]
        for detail in details:
            writer.writerow([detail])
        writer.writerow([])  # Empty row for spacing

        # Write Table Headers
        table_headers = ['Name', 'Layout Type', 'Review Status', 'Reviewers', 'Reviewed Date', 'Layouters', 'Progress']
        writer.writerow(table_headers)

        # Function to parse multiple date formats
        def parse_review_date(review_date):
            if not review_date or review_date == 'N/A':
                return 'N/A'
            try:
                # Try DD/MM/YYYY HH:MM:SS
                parsed_date = datetime.datetime.strptime(review_date, '%d/%m/%Y %H:%M:%S')
                return parsed_date.strftime('%d/%m/%Y %H:%M')  # Output as DD/MM/YYYY HH:MM
            except ValueError:
                try:
                    # Try YYYY-MM-DD HH:MM
                    parsed_date = datetime.datetime.strptime(review_date, '%Y-%m-%d %H:%M')
                    return parsed_date.strftime('%d/%m/%Y %H:%M')  # Output as DD/MM/YYYY HH:MM
                except ValueError:
                    print(f"Error parsing reviewdate '{review_date}' for cell {cell.get('name', 'Unknown')}")
                    return 'N/A'

        # Write Table Data
        for cell in cells:
            # Parse and format reviewdate
            review_date = cell.get('reviewdate', 'N/A')
            formatted_date = parse_review_date(review_date)

            writer.writerow([
                cell.get('name', ''),
                cell.get('layoutType', 'custom').capitalize(),
                cell.get('status', 'not-started').replace('-', ' ').title(),
                cell.get('reviewer', 'N/A'),
                formatted_date,
                cell.get('layouters', 'N/A'),
                f"{cell.get('completionPercentage', 0)}%"
            ])

        # Prepare response
        buffer.seek(0)
        return Response(
            buffer.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment;filename=Project_{project_id}_Report.csv'}
        )
    except Exception as e:
        print(f"Error generating CSV for project {project_id}: {e}")
        return jsonify({'error': str(e)}), 500
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)