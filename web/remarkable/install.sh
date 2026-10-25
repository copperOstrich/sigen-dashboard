#!/bin/sh
# Home Energy sleep screen for a reMarkable 2 (software 3.20 or later).
#
# Run on the tablet over SSH:   wget -qO- http://<collector>/remarkable/install.sh | sh
#
# The tablet still adds the owner name and number from its own settings; the image leaves
# room for them. SLEEP_TITLE changes the heading, in front of "sh":
#   wget -qO- http://<collector>/remarkable/install.sh | SLEEP_TITLE="Home energy" sh
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

# The heading drawn on the image. Re-running the installer keeps the previous one unless a
# new one is given. rm=1 leaves room for the owner details the tablet draws on top.
[ -f "$DIR/settings" ] && . "$DIR/settings"
SLEEP_TITLE="${SLEEP_TITLE-${SAVED_TITLE-Paper tablet is sleeping}}"
# percent-encode with sed only (the tablet's busybox has a minimal od)
urlenc() { printf '%s' "$1" | sed -e 's/%/%25/g' -e 's/ /%20/g' -e 's/&/%26/g' -e 's/#/%23/g' \
  -e 's/+/%2B/g' -e 's/?/%3F/g' -e 's/=/%3D/g' -e "s/'/%27/g" -e 's/"/%22/g'; }
QUERY="rm=1&title=$(urlenc "$SLEEP_TITLE")"
mkdir -p "$DIR"
printf "SAVED_TITLE='%s'\n" "$(printf '%s' "$SLEEP_TITLE" | sed "s/'/'\\\\''/g")" > "$DIR/settings"
wget -q -T 20 -O "$DIR/install.sh" "$BASE/remarkable/install.sh" || true

cat > "$DIR/fetch.sh" <<EOF
#!/bin/sh
# Fetch a fresh energy image; keep the last one if the collector can't be reached.
wget -q -T 20 -O "$DIR/energy.tmp" "$BASE/eink.png?$QUERY" && [ -s "$DIR/energy.tmp" ] && mv "$DIR/energy.tmp" "$DIR/energy.png"
rm -f "$DIR/energy.tmp"
exit 0
EOF
chmod +x "$DIR/fetch.sh"

"$DIR/fetch.sh"
if [ ! -s "$DIR/energy.png" ]; then
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
