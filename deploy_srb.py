#!/usr/bin/env python3
"""Deploy s_r_bot v3 + restart script to the VPS via SFTP."""
import paramiko, sys, os

HOST = "129.226.213.46"
USER = "ubuntu"
PASS = "Julkar2009@12"
REMOTE_DIR = "/home/ubuntu/lighter-grid"

FILES = [
    ("s_r_bot.py", "s_r_bot.py"),
    ("restart_srb.sh", "restart_srb.sh"),
    ("fix_cron.sh", "fix_cron.sh"),
    (".gitignore", ".gitignore"),
]

local_dir = os.path.dirname(os.path.abspath(__file__))
t = paramiko.Transport((HOST, 22))
t.connect(username=USER, password=PASS)
sftp = paramiko.SFTPClient.from_transport(t)
for local, remote in FILES:
    lp = os.path.join(local_dir, local)
    sftp.put(lp, f"{REMOTE_DIR}/{remote}")
    print(f"uploaded {local} -> {REMOTE_DIR}/{remote}")
sftp.close()
t.close()
print("OK")
