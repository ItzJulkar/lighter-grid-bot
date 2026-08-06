#!/bin/bash
# Fix the S/R bot cron watchdog.
# Old:  pgrep -f python3 -u s_r_bot   -> -f pattern="python3", -u s_r_bot = USER FILTER
#       (user "s_r_bot" doesn't exist) -> always exit 1 -> restart every 5 min -> bot never survives.
# New:  pgrep -f 'python3 -u [s]_r_bot' -> -f gets the full pattern; [s] breaks the
#       self-match (cron's own sh -c cmdline contains "[s]_r_bot" literally, which the
#       regex "python3 -u [s]_r_bot" does NOT match).
crontab -l | sed "s|pgrep -f python3 -u s_r_bot|pgrep -f 'python3 -u [s]_r_bot'|" | crontab -
echo "=== crontab after fix ==="
crontab -l | grep lighter
