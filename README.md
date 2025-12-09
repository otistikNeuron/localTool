# Japa Remover A12+ Setup Guide

This guide explains how to deploy the server component on your Docker homelab and run the client script on Windows.

## 1. Server Side (Docker Homelab)

**Path:** `/path/to/your/docker/japaremover`

### 1. Prepare Directory

On your homelab server, create a new directory for this tool:

```bash
mkdir -p /path/to/your/docker/japaremover
```
 
### 2. Transfer Files

Copy the `docker-compose.yml` file and the entire `server` folder into the directory you created. The structure should look like this:

```
/path/to/your/docker/japaremover/
├── docker-compose.yml
└── server/
    ├── Dockerfile
    ├── cron/
    ├── public/
    └── templates/
```

### 3. Start the Server

Navigate to the directory and start the container:

```bash
cd /path/to/your/docker/japaremover
docker compose up -d --build
```

### 4. Verify

The server should now be running on port 8000. Test it by opening `http://<YOUR-SERVER-IP>:8000` in a browser.

## 2. Client Side (Windows)

### 1. Prerequisites

* Ensure Python is installed on your Windows machine.

### 2. Configuration

* Open `downloads.28.png` in a text editor.
* Open `downloads.sqlitedb` in a text editor.
* Open `main.py` in a text editor.
* Find the variable defining the server URL (e.g., `base_url` or similar).
* Change `localhost` to your homelab's IP address (e.g., `http://192.168.1.XX:8000`).

### 3. Install Dependencies

Open a command prompt (cmd) or PowerShell in the folder containing `main.py` and install any required libraries (commonly `requests`):

```bash
pip install requests
```

(Check the `import` statements in `main.py` if other libraries are missing).

### 4. Run the Tool

```bash
python main.py
```

## Maintenance

* **Logs:** To check server logs, run `docker compose logs -f` inside the server directory.

This tool is intended for:

Educational purposes

Security research

Legitimate device recovery

Authorized testing

Users are responsible for complying with local laws and regulations.