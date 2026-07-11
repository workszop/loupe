#!/bin/sh -e
# Build and install loupe. Idempotent — safe to rerun after any change.
#
#   bundle        -> ~/.local/bin/loupe
#   toggle script -> ~/.local/bin/loupe-toggle   (bind to Super+Z)
#   desktop entry -> ~/.local/share/applications/loupe.desktop
#   udev rule     -> /etc/udev/rules.d/70-loupe-uinput.rules  (via pkexec;
#                    skipped on 'n' — click-through then depends on the
#                    steam/distro uaccess rules already present)

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RULE=/etc/udev/rules.d/70-loupe-uinput.rules

python3 "$REPO/tools/build.py"

install -Dm755 "$REPO/loupe.py" "$HOME/.local/bin/loupe"
install -Dm755 "$REPO/tools/loupe-toggle" "$HOME/.local/bin/loupe-toggle"
install -Dm644 "$REPO/tools/loupe.desktop" \
    "$HOME/.local/share/applications/loupe.desktop"
echo "installed: ~/.local/bin/loupe, ~/.local/bin/loupe-toggle, loupe.desktop"

if [ -f "$RULE" ]; then
    echo "udev rule already present: $RULE"
else
    echo "installing $RULE (grants /dev/uinput to the active seat user"
    echo "independently of the steam/distro rules) — authentication prompt..."
    if pkexec sh -c "printf '%s\n' \
        '# loupe: allow the active seat user to create uinput devices' \
        'KERNEL==\"uinput\", SUBSYSTEM==\"misc\", TAG+=\"uaccess\", OPTIONS+=\"static_node=uinput\"' \
        > $RULE && udevadm control --reload && udevadm trigger --name-match=uinput"
    then
        echo "udev rule installed"
    else
        echo "udev rule skipped (click-through still works while the" \
             "steam/distro uaccess rules exist)" >&2
    fi
fi

echo
echo "COSMIC shortcut command for Super+Z:  $HOME/.local/bin/loupe-toggle"
echo "logs:                                 journalctl --user -u loupe"
