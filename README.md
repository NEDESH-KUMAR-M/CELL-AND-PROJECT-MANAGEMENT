<div align="center">


# Cell & Project Management System

**A web-based IC layout project tracking tool — powered by Flask + Google Workspace**

![Python](https://img.shields.io/badge/Python-3.9+-3b82f6?style=flat-square&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-2.x-7b5cff?style=flat-square&logo=flask&logoColor=white)
![Google Sheets](https://img.shields.io/badge/Google_Sheets-Data_Store-00ffe0?style=flat-square&logo=googlesheets&logoColor=white)
![Google Drive](https://img.shields.io/badge/Google_Drive-File_Store-ff4d6d?style=flat-square&logo=googledrive&logoColor=white)
![App Engine](https://img.shields.io/badge/App_Engine-Deploy-f59e0b?style=flat-square&logo=googlecloud&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

</div>

---

## What is this?

A role-based web application built for **IC (Integrated Circuit) layout engineering teams**. Admins manage projects, employees, and cells. Reviewers track layout status and run structured review checklists — all backed by Google Sheets (no database needed) and Google Drive for image storage.

---

## Features

| | Feature | Description |
|---|---|---|
| 📁 | **Project Management** | Create and update projects with client info, version, status, images, and assigned reviewers. Auto end-date stamping on completion. |
| 🔬 | **Cell Layout Tracking** | Add layout cells to each project. Assign layouters, upload images, and track completion percentage. |
| ✅ | **Smart Checklists** | Auto-populated review checklists per layout type (Cell: 32 items, Top: 33, Tape: 30, IO: 10, Custom: flexible). Dropdown validations enforced via Sheets API. |
| 🔐 | **Role-Based Access** | Admins: full control. Reviewers: assigned projects only. Session auth with active/inactive user status. |
| ☁️ | **Google Integration** | All data in Google Sheets. Images in Drive. Apps Script auto-creates a dedicated sheet per new project. |
| 📄 | **PDF Export** | Generate structured PDF reports from project/cell data via ReportLab. |

---

## Checklist Coverage

| Layout Type | Items |
|---|---|
| Cell Layout | 32 |
| Top Layout | 33 |
| Tape Layout | 30 |
| IO Layout | 10 |
| Custom Layout | Flexible |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.9+, Flask, Flask-CORS |
| Data Store | Google Sheets (gspread) |
| File Store | Google Drive API v3 |
| Auth | Google Service Account, OAuth2 |
| Frontend | HTML/CSS/JS, Jinja2 templates |
| Export | ReportLab, CSV |
| Deployment | Google App Engine |

---

## Project Structure

```
CELL-AND-PROJECT-MANAGEMENT/
├── app.py                   # 2197 lines — all routes, APIs, Google integrations
├── app.yaml                 # Google App Engine config
├── requirements.txt
├── sidebar.js
├── static/                  # CSS, JS, assets
└── templates/               # Jinja2 HTML
    ├── login.html
    ├── admin_dashboard.html
    ├── projects.html
    ├── openproject.html
    ├── empdash.html
    ├── empopenprojects.html
    ├── profile.html
    └── eprofile.html
```

---

## Getting Started

### Prerequisites
- Python 3.9+
- Google Cloud project with **Sheets** and **Drive** APIs enabled
- A Service Account JSON key file
- Google Sheet with `users` and `projects` worksheets

### Install

```bash
git clone https://github.com/NEDESH-KUMAR-M/CELL-AND-PROJECT-MANAGEMENT.git
cd CELL-AND-PROJECT-MANAGEMENT
pip install -r requirements.txt
```

### Configure

Set environment variables:

```bash
export GSA_KEY_FILE=your-service-account.json
export SECRET_KEY=your-secret-key
```

Update constants in `app.py`:

```python
SHEET_ID = "your-google-sheet-id"
DRIVE_FOLDER_ID = "your-drive-folder-id"
GOOGLE_SCRIPT_URL = "your-apps-script-url"
```

### Run Locally

```bash
python app.py
# → http://127.0.0.1:5000
```

### Deploy to App Engine

```bash
gcloud app deploy
```

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/projects` | List all projects |
| `POST` | `/api/projects` | Create a project |
| `PUT` | `/api/projects/<id>` | Update a project |
| `GET` | `/api/cells?project_id=<id>` | List cells for a project |
| `POST` | `/api/cells?project_id=<id>` | Add a cell |
| `GET` | `/api/cells/<cell_id>` | Get cell detail |
| `GET` | `/api/employees` | List employees (admin) |
| `POST` | `/admin/add_employee` | Add an employee |
| `PUT` | `/api/profile/password` | Change password |

---

## Roles

| Role | Access |
|---|---|
| **Admin** | Manage projects, cells, employees; full CRUD |
| **Reviewer** | View assigned projects; update cell/checklist status |

---

## License

[MIT](LICENSE) — built by [NEDESH-KUMAR-M](https://github.com/NEDESH-KUMAR-M)
