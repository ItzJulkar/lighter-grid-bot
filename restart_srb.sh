#!/bin/bash
cd ~/lighter-grid
# kill any running instance by exact python match only
for pid in $(pgrep -f "python3 -u s_r_bot"); do kill $pid 2>/dev/null; done
for pid in $(pgrep -f "python3 -u grid_bot"); do kill $pid 2>/dev/null; done
sleep 1
# rotate logs instead of deleting — trade history must survive restarts
[ -f srb.log ] && mv -f srb.log srb.log.1
[ -f srb.out ] && mv -f srb.out srb.out.1
nohup python3 -u s_r_bot.py > srb.out 2>&1 &
echo "started pid $!"
