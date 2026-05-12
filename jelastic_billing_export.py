#!/usr/bin/env python3
"""
Jelastic Billing Export
-----------------------
Pulls billing data from a Jelastic/Virtuozzo cloud platform using the
native CSV export API (same format as the SaveInCloud portal), then
uploads the files via SFTP or FTP.

Designed to run headless on a Linux VM via cron or systemd timer.
All configuration lives in config.yaml (or environment variables).
"""

import os
import sys
import csv
import ftplib
import logging
import logging.handlers
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

# API interval limits per period granularity
PERIOD_MAX_DAYS = {
    "HOUR": 1,
    "DAY": 7,
    "MONTH": 365,
    "YEAR": 3650,
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(config_path="config.yaml"):
    """Load YAML config and overlay environment variable overrides."""
    path = Path(config_path)
    if not path.exists():
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    env_overrides = {
        "JELASTIC_API_URL": ("jelastic", "api_url"),
        "JELASTIC_TOKEN":   ("jelastic", "token"),
        "SFTP_HOST":        ("sftp", "host"),
        "SFTP_USER":        ("sftp", "username"),
        "SFTP_PASS":        ("sftp", "password"),
        "FTP_HOST":         ("ftp", "host"),
        "FTP_USER":         ("ftp", "username"),
        "FTP_PASS":         ("ftp", "password"),
    }
    for env_var, (section, key) in env_overrides.items():
        val = os.environ.get(env_var)
        if val and section in cfg:
            cfg[section][key] = val

    return cfg


def setup_logging(cfg):
    """Configure rotating file + console logging."""
    log_cfg = cfg.get("logging", {})
    log_file = log_cfg.get("log_file", "./logs/billing_export.log")
    level_str = log_cfg.get("level", "INFO").upper()
    max_bytes = log_cfg.get("max_size_mb", 5) * 1024 * 1024
    backup_count = log_cfg.get("backup_count", 3)

    log_dir = Path(log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, level_str, logging.INFO)
    logger = logging.getLogger("jelastic_billing")
    logger.setLevel(level)

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Date range helpers
# ---------------------------------------------------------------------------

def compute_date_range(cfg):
    """Return (start_dt, end_dt) based on config settings."""
    billing = cfg.get("billing", {})
    start_str = billing.get("start_date", "")
    end_str = billing.get("end_date", "")

    if start_str and end_str:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
        return start_dt, end_dt

    lookback = billing.get("lookback", "7d")
    now = datetime.now()
    end_dt = now.replace(hour=23, minute=59, second=59, microsecond=0)

    if lookback.endswith("d"):
        days = int(lookback[:-1])
        start_dt = (now - timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif lookback.endswith("m"):
        months = int(lookback[:-1])
        start_dt = (now.replace(day=1) - timedelta(days=30 * months)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        start_dt = (now - timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    return start_dt, end_dt


def chunk_date_range(start_dt, end_dt, period):
    """Split a date range into chunks that respect API interval limits."""
    max_days = PERIOD_MAX_DAYS.get(period, 7)
    chunks = []
    current = start_dt
    while current < end_dt:
        chunk_end = min(current + timedelta(days=max_days), end_dt)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(seconds=1)
    return chunks


# ---------------------------------------------------------------------------
# Jelastic API
# ---------------------------------------------------------------------------

class JelasticClient:
    """Wrapper around the Jelastic REST API with native CSV export."""

    def __init__(self, api_url, token, logger):
        self.api_url = api_url.rstrip("/") + "/"
        self.token = token
        self.logger = logger
        self.session = requests.Session()

    def _call(self, endpoint, params=None):
        url = f"{self.api_url}{endpoint}"
        if params is None:
            params = {}
        params["session"] = self.token
        self.logger.debug(f"API call: {endpoint}")
        resp = self.session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != 0:
            self.logger.debug(f"API error on {endpoint}: {data.get('error')}")
            return None
        return data

    def get_environments(self):
        """Fetch all environments for the authenticated account."""
        data = self._call("environment/control/rest/getenvs")
        if data is None:
            return []
        envs = []
        for info in data.get("infos", []):
            env = info.get("env", {})
            envs.append({
                "envName": env.get("envName", ""),
                "appid": env.get("appid", ""),
                "domain": env.get("domain", ""),
                "status": env.get("status", 0),
            })
        self.logger.info(f"Found {len(envs)} environment(s)")
        return envs

    def export_billing_csv(self, start_dt, end_dt, period="DAY",
                           group_nodes=False, env_name=None):
        """
        Use the native ExportAccountBillingHistoryByPeriod endpoint to
        get a CSV download link, then download the CSV content.
        Returns the CSV text (including headers).

        Automatically chunks large date ranges and merges the results.
        """
        chunks = chunk_date_range(start_dt, end_dt, period)
        all_lines = []
        header_line = None

        for i, (chunk_start, chunk_end) in enumerate(chunks):
            params = {
                "startTime": chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                "endTime": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
                "period": period,
                "groupNodes": str(group_nodes).lower(),
            }
            if env_name:
                params["envName"] = env_name

            data = self._call(
                "billing/account/rest/exportaccountbillinghistorybyperiod",
                params
            )
            if data is None:
                self.logger.warning(
                    f"Export failed for chunk {chunk_start} to {chunk_end}"
                )
                continue

            download_link = data.get("link", "")
            if not download_link:
                self.logger.warning("No download link in API response")
                continue

            self.logger.debug(f"Downloading CSV from: {download_link}")
            resp = self.session.get(download_link, timeout=120)
            resp.raise_for_status()

            csv_text = resp.text.strip()
            if not csv_text:
                self.logger.debug(f"  Chunk {i+1}/{len(chunks)}: empty CSV")
                continue

            lines = csv_text.split("\n")

            if header_line is None and lines:
                header_line = lines[0]

            # Add data rows (skip header on subsequent chunks)
            data_lines = lines[1:] if i > 0 or header_line else lines
            if i == 0:
                data_lines = lines[1:]

            data_rows = [l for l in data_lines if l.strip()]
            all_lines.extend(data_rows)
            self.logger.debug(
                f"  Chunk {i+1}/{len(chunks)}: {len(data_rows)} data rows"
            )

        if header_line:
            total_rows = len(all_lines)
            self.logger.info(f"CSV export: {total_rows} billing rows fetched")
            return header_line + "\n" + "\n".join(all_lines) if all_lines else header_line + "\n"
        elif all_lines:
            return "\n".join(all_lines)
        else:
            self.logger.warning("No billing data in export")
            return None


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def build_filename(pattern, env_name="", start_dt=None, end_dt=None):
    """Replace placeholders in the filename pattern."""
    now = datetime.now()
    replacements = {
        "{date}": now.strftime("%Y-%m-%d"),
        "{datetime}": now.strftime("%Y-%m-%d_%H%M%S"),
        "{env}": env_name,
        "{start}": start_dt.strftime("%Y-%m-%d") if start_dt else "",
        "{end}": end_dt.strftime("%Y-%m-%d") if end_dt else "",
    }
    result = pattern
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


def save_csv(csv_text, filepath, logger):
    """Save CSV text to a file."""
    out_dir = Path(filepath).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(csv_text)

    line_count = csv_text.count("\n")
    logger.info(f"CSV saved: {filepath} ({line_count} lines)")
    return True


# ---------------------------------------------------------------------------
# Upload: SFTP and FTP
# ---------------------------------------------------------------------------

def upload_via_sftp(filepath, cfg, logger):
    """Upload a file via SFTP (SSH)."""
    if not HAS_PARAMIKO:
        logger.error("paramiko not installed. Run: pip install paramiko")
        return False

    sftp_cfg = cfg.get("sftp", {})
    host = sftp_cfg.get("host", "")
    port = sftp_cfg.get("port", 22)
    user = sftp_cfg.get("username", "")
    password = sftp_cfg.get("password", "")
    key_file = sftp_cfg.get("key_file", "")
    remote_dir = sftp_cfg.get("remote_dir", "/")

    if not host or host == "YOUR_SFTP_HOST":
        logger.warning("SFTP host not configured, skipping upload")
        return False

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        connect_kwargs = {
            "hostname": host,
            "port": port,
            "username": user,
            "timeout": 30,
        }
        if key_file and Path(key_file).exists():
            connect_kwargs["key_filename"] = key_file
        else:
            connect_kwargs["password"] = password

        logger.info(f"Connecting via SFTP: {user}@{host}:{port}")
        ssh.connect(**connect_kwargs)
        sftp = ssh.open_sftp()

        # Ensure remote directory exists
        dirs = remote_dir.strip("/").split("/")
        current = "/"
        for d in dirs:
            if not d:
                continue
            current = f"{current}{d}/"
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current)

        filename = Path(filepath).name
        remote_path = f"{remote_dir.rstrip('/')}/{filename}"
        sftp.put(str(filepath), remote_path)

        sftp.close()
        ssh.close()

        logger.info(f"SFTP upload OK: {filename} -> {remote_path}")

        if sftp_cfg.get("delete_after_upload", False):
            Path(filepath).unlink()
            logger.info(f"Local file deleted: {filepath}")

        return True

    except Exception as e:
        logger.error(f"SFTP upload failed: {e}")
        try:
            ssh.close()
        except Exception:
            pass
        return False


def upload_via_ftp(filepath, cfg, logger):
    """Upload a file via FTP/FTPS."""
    ftp_cfg = cfg.get("ftp", {})
    host = ftp_cfg.get("host", "")
    port = ftp_cfg.get("port", 21)
    user = ftp_cfg.get("username", "")
    password = ftp_cfg.get("password", "")
    use_tls = ftp_cfg.get("use_tls", True)
    remote_dir = ftp_cfg.get("remote_dir", "/")

    if not host or host == "YOUR_FTP_HOST":
        logger.warning("FTP host not configured, skipping upload")
        return False

    try:
        ftp = ftplib.FTP_TLS() if use_tls else ftplib.FTP()
        logger.info(f"Connecting to FTP: {host}:{port}")
        ftp.connect(host, port, timeout=30)
        ftp.login(user, password)
        if use_tls:
            ftp.prot_p()

        for part in remote_dir.strip("/").split("/"):
            if not part:
                continue
            try:
                ftp.cwd(part)
            except ftplib.error_perm:
                ftp.mkd(part)
                ftp.cwd(part)

        filename = Path(filepath).name
        with open(filepath, "rb") as f:
            ftp.storbinary(f"STOR {filename}", f)

        ftp.quit()
        logger.info(f"FTP upload OK: {filename} -> {remote_dir}")

        if ftp_cfg.get("delete_after_upload", False):
            Path(filepath).unlink()

        return True

    except Exception as e:
        logger.error(f"FTP upload failed: {e}")
        return False


def upload_file(filepath, cfg, logger):
    """Upload a file using the configured method (SFTP or FTP)."""
    sftp_cfg = cfg.get("sftp", {})
    ftp_cfg = cfg.get("ftp", {})

    if sftp_cfg.get("enabled", False):
        return upload_via_sftp(filepath, cfg, logger)
    elif ftp_cfg.get("enabled", False):
        return upload_via_ftp(filepath, cfg, logger)
    else:
        logger.info("No upload method enabled, CSV saved locally only")
        return True


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run(config_path="config.yaml"):
    """Main entry point: export billing CSV and upload."""
    cfg = load_config(config_path)
    logger = setup_logging(cfg)

    logger.info("=" * 60)
    logger.info("Jelastic Billing Export - Starting")
    logger.info("=" * 60)

    token = cfg["jelastic"]["token"]
    if not token or token == "YOUR_TOKEN_HERE":
        logger.error(
            "Jelastic API token not configured. "
            "Edit config.yaml or set JELASTIC_TOKEN."
        )
        return False

    start_dt, end_dt = compute_date_range(cfg)
    logger.info(f"Date range: {start_dt} to {end_dt}")

    client = JelasticClient(
        api_url=cfg["jelastic"]["api_url"],
        token=token,
        logger=logger,
    )

    billing_cfg = cfg.get("billing", {})
    csv_cfg = cfg.get("csv", {})
    period = billing_cfg.get("period", "DAY")
    group_nodes = billing_cfg.get("group_nodes", False)
    output_dir = csv_cfg.get("output_dir", "./output")
    pattern = csv_cfg.get("filename_pattern", "billing_{date}.csv")
    per_env = csv_cfg.get("per_environment", False)

    # List environments
    envs = client.get_environments()
    if not envs:
        logger.error("No environments found. Check your token and API URL.")
        return False

    for env in envs:
        logger.info(
            f"  Environment: {env['envName']} "
            f"(domain: {env['domain']}, status: {env['status']})"
        )

    files_written = []

    if per_env:
        # One CSV per environment
        for env in envs:
            env_name = env["envName"]
            logger.info(f"Exporting billing for: {env_name}")

            csv_text = client.export_billing_csv(
                start_dt, end_dt,
                period=period,
                group_nodes=group_nodes,
                env_name=env_name,
            )

            if csv_text:
                filename = build_filename(
                    pattern, env_name=env_name,
                    start_dt=start_dt, end_dt=end_dt
                )
                if "{env}" not in csv_cfg.get("filename_pattern", ""):
                    stem = Path(filename).stem
                    ext = Path(filename).suffix
                    filename = f"{stem}_{env_name}{ext}"

                filepath = str(Path(output_dir) / filename)
                if save_csv(csv_text, filepath, logger):
                    files_written.append(filepath)
    else:
        # All environments in one CSV
        logger.info("Exporting billing for all environments")
        csv_text = client.export_billing_csv(
            start_dt, end_dt,
            period=period,
            group_nodes=group_nodes,
        )

        if csv_text:
            filename = build_filename(
                pattern, start_dt=start_dt, end_dt=end_dt
            )
            filepath = str(Path(output_dir) / filename)
            if save_csv(csv_text, filepath, logger):
                files_written.append(filepath)

    if not files_written:
        logger.warning(
            "No CSV files generated. "
            "The account may have no billing data for this period."
        )

    # Upload files
    upload_count = 0
    for fpath in files_written:
        if upload_file(fpath, cfg, logger):
            upload_count += 1

    # Summary
    logger.info("-" * 60)
    logger.info(f"Done: {len(files_written)} CSV file(s) created")
    sftp_on = cfg.get("sftp", {}).get("enabled", False)
    ftp_on = cfg.get("ftp", {}).get("enabled", False)
    if sftp_on or ftp_on:
        method = "SFTP" if sftp_on else "FTP"
        logger.info(f"{method} uploads: {upload_count}/{len(files_written)} OK")
    logger.info("=" * 60)

    return len(files_written) > 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Jelastic Billing Export - Export billing CSV and upload"
    )
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)"
    )
    args = parser.parse_args()

    ok = run(config_path=args.config)
    sys.exit(0 if ok else 1)
