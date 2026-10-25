#!/bin/sh
# Home Energy sleep screen for a reMarkable 2 (software 3.20 or later).
#
# Run on the tablet over SSH:   wget -qO- http://<collector>/remarkable/install.sh | sh
#
# The tablet still adds the owner name and number from its own settings; the image leaves
# room for them. Options go in front of "sh": SLEEP_TITLE changes the heading, AWAY_HOURS how
# long without reaching the collector before an "away from home" screen replaces old figures (3):
#   wget -qO- http://<collector>/remarkable/install.sh | SLEEP_TITLE="Home energy" AWAY_HOURS=6 sh
# Undo everything:              sh /home/root/home-energy/uninstall.sh
# After a reMarkable software update, run the first line again (updates remove the timer).
#
# What it does: keeps a fresh energy image in /home/root/home-energy (refreshed every
# 5 minutes while the tablet is awake) and points the sleep screen at it. Nothing else changes.
set -e
BASE="__BASE__"
DIR=/home/root/home-energy
CONF=/home/root/.config/remarkable/xochitl.conf

echo "Setting up the Home Energy sleep screen from $BASE"

# The heading drawn on the image, and how long without reaching the collector before the
# tablet shows an "away from home" screen instead of old figures. Re-running the installer
# keeps the previous values unless new ones are given. rm=1 leaves room for the owner details
# the tablet draws on top.
[ -f "$DIR/settings" ] && . "$DIR/settings"
SLEEP_TITLE="${SLEEP_TITLE-${SAVED_TITLE-Paper tablet is sleeping}}"
AWAY_HOURS="${AWAY_HOURS-${SAVED_AWAY_HOURS-3}}"
case "$AWAY_HOURS" in ''|*[!0-9]*) echo "AWAY_HOURS must be a whole number of hours"; exit 1 ;; esac
# percent-encode with sed only (the tablet's busybox has a minimal od)
urlenc() { printf '%s' "$1" | sed -e 's/%/%25/g' -e 's/ /%20/g' -e 's/&/%26/g' -e 's/#/%23/g' \
  -e 's/+/%2B/g' -e 's/?/%3F/g' -e 's/=/%3D/g' -e "s/'/%27/g" -e 's/"/%22/g'; }
QUERY="rm=1&title=$(urlenc "$SLEEP_TITLE")"
mkdir -p "$DIR"
{
  printf "SAVED_TITLE='%s'\n" "$(printf '%s' "$SLEEP_TITLE" | sed "s/'/'\\\\''/g")"
  printf "SAVED_AWAY_HOURS=%s\n" "$AWAY_HOURS"
} > "$DIR/settings"
wget -q -T 20 -O "$DIR/install.sh" "$BASE/remarkable/install.sh" || true

# only a real PNG counts (a hotel or cafe Wi-Fi login page, or some other device, doesn't)
ispng() { [ "$(dd if="$1" bs=1 skip=1 count=3 2>/dev/null)" = "PNG" ]; }

# the away screen, kept on the tablet for when the collector can't be reached
if wget -q -T 20 -O "$DIR/away.tmp" "$BASE/eink.png?away=1&$QUERY" && ispng "$DIR/away.tmp"; then
  mv "$DIR/away.tmp" "$DIR/away.png"
else
  rm -f "$DIR/away.tmp"
  echo "Couldn't download $BASE/eink.png. Check the tablet is on the same Wi-Fi and the address is right."
  exit 1
fi

cat > "$DIR/fetch.sh" <<EOF
#!/bin/sh
# Fetch a fresh energy image. Only a real PNG is used, so a Wi-Fi login page can't replace it.
# If the collector hasn't been reached for $AWAY_HOURS hours (away from home, or a network that
# blocks it), show the away screen instead of old figures; back home, the next fetch replaces it.
D=$DIR
ispng() { [ "\$(dd if="\$1" bs=1 skip=1 count=3 2>/dev/null)" = "PNG" ]; }
now=\$(date +%s)
if wget -q -T 20 -O "\$D/energy.tmp" "$BASE/eink.png?$QUERY" 2>/dev/null && ispng "\$D/energy.tmp"; then
  mv "\$D/energy.tmp" "\$D/energy.png"
  echo "\$now" > "\$D/last-ok"
  rm -f "\$D/away-shown"
else
  rm -f "\$D/energy.tmp"
  last=\$(cat "\$D/last-ok" 2>/dev/null || echo 0)
  if [ \$((now - last)) -gt $((AWAY_HOURS * 3600)) ] && [ ! -f "\$D/away-shown" ] && ispng "\$D/away.png"; then
    cp "\$D/away.png" "\$D/energy.png" && touch "\$D/away-shown"
  fi
fi
exit 0
EOF
chmod +x "$DIR/fetch.sh"

before=$(cat "$DIR/last-ok" 2>/dev/null || true)
"$DIR/fetch.sh"
if [ "$(cat "$DIR/last-ok" 2>/dev/null || true)" = "$before" ] || [ ! -s "$DIR/energy.png" ]; then
  echo "Couldn't download $BASE/eink.png. Check the tablet is on the same Wi-Fi and the address is right."
  exit 1
fi

cat > "$DIR/home-energy.service" <<EOF
[Unit]
Description=Fetch the Home Energy sleep screen
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=$DIR/fetch.sh
EOF
cat > "$DIR/home-energy.timer" <<EOF
[Unit]
Description=Refresh the Home Energy sleep screen every 5 minutes while awake

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
cp "$DIR/home-energy.service" "$DIR/home-energy.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now home-energy.timer >/dev/null 2>&1

# Point the sleep screen at the image (the original settings file is kept first)
[ -f "$DIR/xochitl.conf.before" ] || cp "$CONF" "$DIR/xochitl.conf.before"
if grep -q '^SleepScreenPath=' "$CONF"; then
  sed -i "s|^SleepScreenPath=.*|SleepScreenPath=$DIR/energy.png|" "$CONF"
else
  sed -i "/^\[General\]/a SleepScreenPath=$DIR/energy.png" "$CONF"
fi

cat > "$DIR/uninstall.sh" <<'EOF'
#!/bin/sh
# Remove the Home Energy sleep screen and put the normal one back.
DIR=/home/root/home-energy
CONF=/home/root/.config/remarkable/xochitl.conf
systemctl disable --now home-energy.timer >/dev/null 2>&1
rm -f /etc/systemd/system/home-energy.service /etc/systemd/system/home-energy.timer
systemctl daemon-reload
sed -i '\|^SleepScreenPath=/home/root/home-energy/|d' "$CONF"
# if a different custom sleep screen was set before, put it back
OLD=$(grep '^SleepScreenPath=' "$DIR/xochitl.conf.before" 2>/dev/null | head -n 1)
[ -n "$OLD" ] && sed -i "/^\[General\]/a $OLD" "$CONF"
rm -rf "$DIR"
systemctl restart xochitl
echo "Removed. The normal sleep screen is back."
EOF
chmod +x "$DIR/uninstall.sh"

echo "Done. Restarting the reMarkable interface so it picks up the new sleep screen..."
systemctl restart xochitl
echo "Put the tablet to sleep to see it. To undo: sh $DIR/uninstall.sh"
