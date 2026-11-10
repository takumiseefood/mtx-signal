#!/usr/bin/env bash
# 約 5 小時 45 分的迴圈：交易時段每 5 分鐘、其他時間每 30 分鐘執行一次 signal.py
set -u
END=$((SECONDS + 20700))
git config user.name "mtx-signal-bot"
git config user.email "actions@users.noreply.github.com"
mkdir -p site
git fetch -q origin live 2>/dev/null && git show origin/live:sim60_live.json > site/sim60_live.json 2>/dev/null || rm -f site/sim60_live.json

trading_hours() {   # 台北時間：週一~五 08:30-14:59 日盤；週一~五 15:00 起、到隔天 05:10 夜盤
  local dow hm
  dow=$(TZ=Asia/Taipei date +%u); hm=$(TZ=Asia/Taipei date +%H%M); hm=$((10#$hm))
  if [ "$dow" -le 5 ] && [ "$hm" -ge 830 ]; then return 0; fi
  if [ "$dow" -ge 2 ] && [ "$dow" -le 6 ] && [ "$hm" -le 510 ]; then return 0; fi
  return 1
}

publish_live() {    # data.json 放到 live 分支（只保留最新一版），網頁直接讀取
  rm -rf /tmp/live && mkdir -p /tmp/live && cp site/data.json /tmp/live/ || return; cp site/sim60_live.json /tmp/live/ 2>/dev/null
  (cd /tmp/live && git init -q -b live && git add -A && \
   git -c user.name=mtx-signal-bot -c user.email=actions@users.noreply.github.com commit -qm "live $(TZ=Asia/Taipei date '+%F %R')" && \
   git push -f -q "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" live) || echo "live 發佈失敗"
}

while [ $SECONDS -lt $END ]; do
  git pull -q --rebase origin main || git rebase --abort 2>/dev/null
  echo "── $(TZ=Asia/Taipei date '+%F %T')"
  python signal.py || echo "signal.py 失敗"
  python sim60.py || echo "sim60.py 失敗"
  git add data/
  if ! git diff --cached --quiet; then
    git commit -qm "update data $(TZ=Asia/Taipei date '+%F %R')" && (git push -q origin HEAD:main || (git pull -q --rebase origin main && git push -q origin HEAD:main))
  fi
  publish_live
  if trading_hours; then step=300; else step=1800; fi
  now=$(date +%s); wait=$(( step - now % step + 20 ))
  if trading_hours; then python watch.py "$wait" || sleep "$wait"; else sleep "$wait"; fi   # 盤中每 10 秒盯價
done
