#!/usr/bin/env python3
"""
AuraPi - GUI-Tool für Ubuntu/Linux zum Sichern und Zurückspielen von
SD-Karten & USB-Sticks (z.B. Raspberry-Pi-Systeme).

ÜBERBLICK
---------
Python 3 + CustomTkinter (Dark Mode, Navy-Farbtheme, siehe
aurapi_navy_theme.json). Drei Backup-Modi (Raspberry Pi OS / Other OS /
Vollständiges Abbild) und ein Restore-Modus, jeweils mit eigenem Tab.
Wortmarke und Icon sind als Base64-PNG direkt im Code eingebettet (siehe
_WORDMARK_PNG_B64 / _ICON_STILL_B64 / _ICON_ANIM_B64) - die App ist damit
eine einzelne, eigenständige .py-Datei ohne externe Bild-Assets. Einzige
externe Datei ist das Farbtheme (aurapi_navy_theme.json), das im selben
Verzeichnis liegen muss.

SICHERHEITSARCHITEKTUR (aktueller Stand)
-----------------------------------------
- PiShrink wird kryptografisch verifiziert: gepinnter Commit + SHA-256-Hash
  (PISHRINK_PINNED_SHA256/_COMMIT unten) + Prüfung von Besitzer (root) und
  Schreibrechten (nicht group/other-writable) vor jeder Ausführung. Kein
  relativer PATH-Kandidat, nur /usr/local/bin/pishrink.sh.
- Backup schreibt die Zieldatei nicht als Root: pkexec/dd liest nur
  (stdout-Pipe), ein unprivilegierter Python-Prozess schreibt die Datei.
  Root bekommt dadurch keinen beliebigen Dateischreibzugriff.
- Backup-Ziel wird atomar geschrieben (*.part -> rename erst bei Erfolg),
  Symlinks als Ziel werden abgelehnt, vorhandene Dateien nur mit
  ausdrücklicher Bestätigung überschrieben. Bei Abbruch durch den Nutzer
  (siehe _cancel_event) werden unfertige .part-Dateien automatisch gelöscht.
- Geräte ohne Seriennummer UND ohne /dev/disk/by-id-Pfad werden gar nicht
  erst zur Auswahl angeboten (fail closed statt fail open).
- Alle Geräte-Operationen verwenden konsequent den by-id-Pfad, nicht /dev/sdX.
- Erneute Geräte-Verifikation direkt nach dem Aushängen, unmittelbar vor
  dem eigentlichen Schreibvorgang (zusätzlich zur Verifikation bei Auswahl).
- Prüfung, ob die Image-Quelldatei physisch auf dem Restore-Zielgerät liegt
  (via findmnt + lsblk-Abstammung) - verhindert Lesen-während-Überschreiben.
- .gz-Images werden vor dem Restore einmal vollständig dekomprimiert, nur
  um die echte Größe zu zählen (Validierungs-Durchlauf), statt sich auf die
  unzuverlässige gzip-Trailer-Größe zu verlassen.
- Stdin-Pipe-Schreiber für den Restore läuft mit separatem Stdout/Stderr-
  Drain-Thread (kein Deadlock-Risiko bei vollen Pipe-Buffern).
- Vor dem Restore-dd wird geprüft, dass der aufrufende Nutzer die
  Image-Datei bereits selbst lesen darf (kein Missbrauch von Root, um
  Dateien zu lesen, auf die der Nutzer sonst keinen Zugriff hätte).
- Root-Rechte werden über einen eigenen 'sudo -A'-Askpass-Dialog angefragt
  (nicht pkexec, siehe ASKPASS_SCRIPT) - sudo cached die Anmeldung, dadurch
  reicht bei langen Vorgängen eine einzige Passwortabfrage.

GUI-ARCHITEKTUR
----------------
- Responsives Layout: CTkScrollableFrame als Wurzel-Container, Scrollbar
  blendet sich nur ein, wenn Inhalt tatsächlich nicht in die Fensterhöhe
  passt (siehe _update_scrollbar_visibility). Icon-Größe und Abstände
  zwischen Abschnitten passen sich beim Verkleinern des Fensters stufenlos
  an (siehe _apply_responsive_layout).
- Icon-Container existiert in Backup- und Restore-Tab je einmal, mit fest
  identischem linkem Abstand (_ACTION_FRAME_LEFT_PADX) statt zentriert -
  garantiert exakt dieselbe X-Position auf beiden Tabs, unabhängig von
  eventuell leicht unterschiedlichen Spaltenbreiten der beiden Tab-Frames.
  Beide Icons werden synchron aktualisiert (siehe set_icon_still() /
  set_icon_animated_gif()). Die Lotus-Animation läuft automatisch während
  eines laufenden Backups/Restores.
- CLI-Log-Bereich ist standardmäßig zugeklappt (siehe _toggle_cli_log) für
  ein kompaktes, aufgeräumtes Standard-Interface.

GEPLANT (noch nicht umgesetzt)
--------------------------------
- Separate ausführliche Dokumentation (Architektur, Installationsanleitung
  für Endnutzer) - vorgemerkt, aktuell nicht Teil dieser Datei.

ABHÄNGIGKEITEN
----------------
  sudo apt install python3-tk python3-pil.imagetk policykit-1 e2fsprogs \\
      parted gzip util-linux
  pip install customtkinter --break-system-packages   (getestet: 6.0.0)

PISHRINK-INSTALLATION (manuell, wird beim Programmstart automatisch geprüft)
  curl -fsSL \\
    https://raw.githubusercontent.com/Drewsif/PiShrink/5f358d03eed4b7334657ee93867826a2b42f112a/pishrink.sh \\
    -o /tmp/pishrink.sh
  sha256sum /tmp/pishrink.sh
  # Muss exakt sein: 71026f0c02ac099e588a3eb8f70760c1b680aa8ea3acde61a0141fbaeb68c777
  sudo install -o root -g root -m 755 /tmp/pishrink.sh /usr/local/bin/pishrink.sh

  Für eine neuere PiShrink-Version: Diff selbst gegen den gepinnten Commit
  prüfen, dann PISHRINK_PINNED_SHA256/_COMMIT unten bewusst aktualisieren.

WARNUNG: Trotz aller Härtungen schreibt dieses Tool mit Root-Rechten auf
Blockgeräte. Nur auf vertrauenswürdigen Rechnern verwenden.
"""

import gzip
import hashlib
import json
import base64
import io
import os
import shutil
import stat
import subprocess
import signal
import sys
import tempfile
import threading
import time
import queue
import zlib
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageSequence
import customtkinter as ctk

GZIP_ERRORS = (OSError, EOFError, gzip.BadGzipFile, zlib.error)

if os.geteuid() == 0:
    sys.exit("Bitte NICHT als root starten. Das Tool fordert Root-Rechte "
              "gezielt per sudo -A selbst an.")

PISHRINK_PATH = "/usr/local/bin/pishrink.sh"
PISHRINK_PINNED_COMMIT = "5f358d03eed4b7334657ee93867826a2b42f112a"
PISHRINK_PINNED_SHA256 = "71026f0c02ac099e588a3eb8f70760c1b680aa8ea3acde61a0141fbaeb68c777"

PISHRINK_INSTALL_HINT = f"""PiShrink ist nicht installiert oder nicht verifizierbar.

Manuelle Installation der GEPRÜFTEN, gepinnten Version:

  curl -fsSL \\
    https://raw.githubusercontent.com/Drewsif/PiShrink/{PISHRINK_PINNED_COMMIT}/pishrink.sh \\
    -o /tmp/pishrink.sh
  sha256sum /tmp/pishrink.sh
  # muss exakt sein: {PISHRINK_PINNED_SHA256}
  sudo install -o root -g root -m 755 /tmp/pishrink.sh {PISHRINK_PATH}

Danach im Tool auf "Erneut prüfen" klicken."""


class SafetyError(Exception):
    pass


class OperationCancelled(Exception):
    """Wird ausgelöst, wenn der Nutzer einen laufenden Backup-/Restore-
    Vorgang aktiv über den 'Abbrechen'-Button beendet hat (im Unterschied zu
    SafetyError, das für automatisch erkannte Sicherheitsprobleme steht)."""
    pass


# --------------------------------------------------------------------------
# Binaries auflösen
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Root-Rechte: sudo -A statt pkexec
# --------------------------------------------------------------------------
# pkexec fragt bei JEDER einzelnen Aktion neu nach dem Passwort (PolicyKit-
# Design, nicht konfigurierbar). sudo cached Anmeldedaten dagegen
# standardmäßig ~15 Minuten sitzungsweit - genau das gewünschte Verhalten
# "einmal eingeben, gilt für den Rest der Sitzung". Der Passwort-Dialog
# selbst wird über SUDO_ASKPASS auf ein eigenes, zum App-Look passendes
# Skript umgeleitet, statt den nativen PolicyKit-Dialog zu zeigen.

ASKPASS_SCRIPT = '''#!/usr/bin/env python3
import sys
import tkinter as tk

def main():
    root = tk.Tk()
    root.title("AuraPi — Root-Rechte")
    root.attributes("-topmost", True)
    root.configure(bg="#1a1a2e")
    root.resizable(False, False)
    w, h = 440, 230
    root.update_idletasks()
    ws = root.winfo_screenwidth(); hs = root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(ws - w) // 2}+{(hs - h) // 2}")

    result = {"pw": None}
    frame = tk.Frame(root, bg="#1a1a2e")
    frame.pack(fill="both", expand=True, padx=24, pady=22)

    tk.Label(frame, text="AuraPi benötigt Root-Rechte",
             bg="#1a1a2e", fg="#e8e8e8", font=("Sans", 13, "bold")).pack(anchor="w")
    tk.Label(frame, text="Bitte dein Benutzerpasswort eingeben (gilt für die\\n"
                          "gesamte Sitzung, keine wiederholte Abfrage nötig):",
             bg="#1a1a2e", fg="#a0a0b0", font=("Sans", 10), justify="left").pack(
        anchor="w", pady=(6, 14))

    entry = tk.Entry(frame, show="*", bg="#252540", fg="#ffffff",
                      insertbackground="#ffffff", relief="flat",
                      highlightthickness=2, highlightbackground="#3a7ebf",
                      highlightcolor="#5b9bd5", font=("Sans", 12))
    entry.pack(fill="x", ipady=8)
    entry.focus_set()

    def submit(event=None):
        result["pw"] = entry.get()
        root.destroy()

    def cancel(event=None):
        result["pw"] = None
        root.destroy()

    entry.bind("<Return>", submit)
    root.bind("<Escape>", cancel)

    btns = tk.Frame(frame, bg="#1a1a2e")
    btns.pack(fill="x", pady=(18, 0))
    tk.Button(btns, text="Abbrechen", command=cancel, bg="#333350", fg="#e0e0e0",
              relief="flat", padx=16, pady=7, activebackground="#444470",
              activeforeground="#ffffff").pack(side="right", padx=(10, 0))
    tk.Button(btns, text="Bestaetigen", command=submit, bg="#3a7ebf", fg="#ffffff",
              relief="flat", padx=16, pady=7, activebackground="#5b9bd5",
              activeforeground="#ffffff").pack(side="right")

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()

    if result["pw"] is None:
        sys.exit(1)
    sys.stdout.write(result["pw"])
    sys.stdout.flush()

if __name__ == "__main__":
    main()
'''

_ASKPASS_HELPER_PATH = None


def install_askpass_helper():
    """Schreibt den Askpass-Helfer in eine private Temp-Datei und setzt
    SUDO_ASKPASS darauf. Muss einmal beim Programmstart aufgerufen werden."""
    global _ASKPASS_HELPER_PATH
    fd, path = tempfile.mkstemp(prefix="aurapi_askpass_", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(ASKPASS_SCRIPT)
    os.chmod(path, 0o700)
    _ASKPASS_HELPER_PATH = path
    os.environ["SUDO_ASKPASS"] = path
    return path


def cleanup_askpass_helper():
    if _ASKPASS_HELPER_PATH and os.path.exists(_ASKPASS_HELPER_PATH):
        try:
            os.remove(_ASKPASS_HELPER_PATH)
        except Exception:
            pass


def privileged(args):
    """Ersetzt frühere pkexec-Aufrufe durch 'sudo -A <args>'. -A sorgt
    dafür, dass sudo den in SUDO_ASKPASS hinterlegten GUI-Dialog statt
    eines Terminal-Prompts nutzt."""
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("sudo nicht gefunden.")
    return [sudo, "-A"] + args


SUDO_KEEPALIVE_INTERVAL_SECONDS = 60


def prime_sudo_session():
    """Fordert EINMALIG am Anfang eines Backup-/Restore-Vorgangs die
    Root-Authentifizierung an und hält die sudo-Sitzung danach mit einem
    Hintergrund-Thread aktiv (periodisches 'sudo -n -v', alle 60s).

    Grund: sudo cached Anmeldedaten standardmäßig nur ~15 Minuten - bei
    Vorgängen, die (wie in der Praxis beobachtet) 20-40+ Minuten dauern,
    liefe die Sitzung sonst mitten im Backup ab und würde erneut nach dem
    Passwort fragen. Der Keep-Alive hält sie durchgehend aktiv, ohne die
    System-weite sudoers-Konfiguration zu verändern.

    Gibt ein threading.Event zurück - mit .set() wird der Keep-Alive-Thread
    sauber beendet, wenn der Vorgang abgeschlossen ist."""
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("sudo nicht gefunden.")
    r = subprocess.run([sudo, "-A", "-v"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Root-Authentifizierung fehlgeschlagen: {r.stderr.strip()}")

    stop_event = threading.Event()

    def keepalive():
        while not stop_event.wait(SUDO_KEEPALIVE_INTERVAL_SECONDS):
            subprocess.run([sudo, "-n", "-v"], capture_output=True)

    threading.Thread(target=keepalive, daemon=True).start()
    return stop_event


def require_bin(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Benötigtes Programm '{name}' wurde nicht gefunden.")
    return path


# --------------------------------------------------------------------------
# PiShrink: kryptografische Verifikation vor jeder Ausführung
# --------------------------------------------------------------------------

def verify_pishrink():
    """Gibt (True, pfad) zurück, nur wenn Hash, Besitzer und Rechte passen."""
    path = PISHRINK_PATH
    if not os.path.isfile(path):
        return False, "nicht gefunden"
    try:
        st = os.stat(path)
    except OSError as e:
        return False, f"stat fehlgeschlagen: {e}"
    if st.st_uid != 0:
        return False, f"gehört nicht root (uid={st.st_uid})"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False, "ist von Gruppe/Andere beschreibbar"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if digest != PISHRINK_PINNED_SHA256:
        return False, f"Hash stimmt nicht überein (gefunden: {digest[:16]}…)"
    return True, path


# --------------------------------------------------------------------------
# lsblk-Baum
# --------------------------------------------------------------------------

def _lsblk_json(fields):
    lsblk = require_bin("lsblk")
    out = subprocess.run([lsblk, "-J", "-b", "-o", fields],
                          capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout).get("blockdevices", [])


def get_lsblk_tree():
    """Versucht MOUNTPOINTS (mehrere Mounts, util-linux >=2.37), fällt bei
    älteren Systemen auf MOUNTPOINT zurück."""
    base = "NAME,PATH,SIZE,MODEL,SERIAL,TRAN,RM,TYPE,FSTYPE,PKNAME"
    try:
        return _lsblk_json(base + ",MOUNTPOINTS")
    except Exception:
        return _lsblk_json(base + ",MOUNTPOINT")


def _mountpoints_of(node):
    mp = node.get("mountpoints")
    if mp is not None:
        return [m for m in mp if m]
    single = node.get("mountpoint")
    return [single] if single else []


_SYSTEM_MOUNTPOINTS = {"/", "/boot", "/boot/efi", "/home", "/var", "/usr",
                       "/opt", "/tmp", "/root", "/srv", "[SWAP]"}
_REMOVABLE_MOUNT_PREFIXES = ("/media/", "/run/media/", "/mnt/")


def _is_system_mountpoint(mp):
    """Unterscheidet system-kritische Mountpoints (Systemplatte, /home, ...)
    von normalen Auto-Mounts externer Wechseldatenträger (typischerweise
    unter /media/<user>/... oder /run/media/<user>/...). Nur Ersteres
    schließt ein Gerät aus der Auswahl aus - ein ganz normal per udisks/GVFS
    eingehängter USB-Stick oder eine SD-Karte darf weiterhin ausgewählt
    werden, sonst wäre praktisch jede frisch eingesteckte Karte blockiert."""
    if not mp:
        return False
    if mp in _SYSTEM_MOUNTPOINTS:
        return True
    if mp.startswith(_REMOVABLE_MOUNT_PREFIXES):
        return False
    # Unbekannter/unerwarteter Mountpoint außerhalb der üblichen Muster:
    # im Zweifel weiterhin vorsichtig ausschließen (fail closed).
    return True


def subtree_is_busy(node):
    fstype = (node.get("fstype") or "").lower()
    if any(_is_system_mountpoint(mp) for mp in _mountpoints_of(node)):
        return True
    if fstype in ("lvm2_member", "linux_raid_member", "crypto_luks",
                  "zfs_member", "zfs"):
        return True
    if node.get("type") in ("lvm", "crypt", "raid0", "raid1", "raid5",
                             "raid6", "raid10", "md"):
        return True
    for child in node.get("children", []) or []:
        if subtree_is_busy(child):
            return True
    return False


def get_by_id_path(devpath):
    by_id_dir = "/dev/disk/by-id"
    if not os.path.isdir(by_id_dir):
        return None
    real_target = os.path.realpath(devpath)
    candidates = []
    try:
        for name in os.listdir(by_id_dir):
            full = os.path.join(by_id_dir, name)
            if os.path.realpath(full) == real_target:
                candidates.append(name)
    except Exception:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda n: (not n.startswith("usb-"), n))
    return os.path.join(by_id_dir, candidates[0])


def human_size(n):
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def list_removable_devices():
    """Nur Wechseldatenträger, die (a) nachweislich nicht Teil des Systems
    sind (LVM/RAID/LUKS/ZFS/gemountet) UND (b) stabil identifizierbar sind
    (Seriennummer + by-id-Pfad vorhanden). Fehlt (b), wird das Gerät NICHT
    angezeigt (fail closed)."""
    devices = []
    try:
        tree = get_lsblk_tree()
    except Exception:
        return devices

    for node in tree:
        if node.get("type") != "disk":
            continue
        if subtree_is_busy(node):
            continue
        tran = (node.get("tran") or "").lower()
        removable = node.get("rm") in (True, "1", 1)
        if not (removable or tran in ("usb", "mmc")):
            continue
        devpath = node.get("path") or f"/dev/{node['name']}"
        serial = (node.get("serial") or "").strip()
        by_id = get_by_id_path(devpath)
        if not serial or not by_id:
            # Fail closed: ohne stabile Identität keine Freigabe.
            continue
        size_bytes = int(node.get("size") or 0)
        model = (node.get("model") or "").strip()
        devices.append({
            "device": by_id,          # <- ab jetzt konsequent der by-id-Pfad
            "raw_device": devpath,    # nur zur Anzeige/zum Vergleich
            "size_bytes": size_bytes,
            "model": model,
            "serial": serial,
            "display": (f"{devpath}  —  {human_size(size_bytes)}  —  "
                        f"{model or tran.upper() or 'Wechseldatenträger'}"
                        f"  (S/N {serial})")
        })
    return devices


def device_children(devpath):
    try:
        tree = get_lsblk_tree()
    except Exception:
        return []
    real = os.path.realpath(devpath)
    for node in tree:
        if os.path.realpath(node.get("path") or f"/dev/{node['name']}") == real:
            out = []

            def collect(n):
                p = n.get("path") or f"/dev/{n['name']}"
                if p != real:
                    out.append(p)
                for c in n.get("children", []) or []:
                    collect(c)
            collect(node)
            return out
    return []


def disk_of_partition(part_path):
    """Ermittelt das übergeordnete Laufwerk (pkname-Kette) einer Partition."""
    try:
        tree = get_lsblk_tree()
    except Exception:
        return None
    real = os.path.realpath(part_path)

    def find(nodes, parent_path=None):
        for n in nodes:
            p = n.get("path") or f"/dev/{n['name']}"
            if os.path.realpath(p) == real:
                return parent_path or p
            children = n.get("children", []) or []
            if children:
                r = find(children, p)
                if r:
                    return r
        return None
    return find(tree)


def image_is_on_device(image_path, target_device_realpath):
    """Prüft, ob die Image-Datei physisch auf dem Restore-Zielgerät liegt.

    Gibt einen von drei Zuständen zurück: 'on', 'off' oder 'unknown'.
    Der Aufrufer MUSS bei 'unknown' den Restore ablehnen (fail closed) -
    nicht nur bei einem sicher festgestellten 'on'. Ein nicht feststellbares
    Ergebnis ist kein Freibrief, sondern ein Grund zur Vorsicht."""
    findmnt = shutil.which("findmnt")
    if not findmnt:
        return "unknown", "findmnt nicht verfügbar"
    d = os.path.dirname(os.path.abspath(image_path)) or "/"
    r = subprocess.run([findmnt, "-no", "SOURCE", "-T", d],
                        capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return "unknown", "Quell-Laufwerk der Image-Datei konnte nicht ermittelt werden"
    source = r.stdout.strip()
    disk = disk_of_partition(source)
    if disk is None:
        return "unknown", f"Übergeordnetes Laufwerk von {source} konnte nicht ermittelt werden"
    if os.path.realpath(disk) == target_device_realpath:
        return "on", disk
    return "off", disk


def reverify_device(expected):
    current = {d["device"]: d for d in list_removable_devices()}
    dev = expected["device"]
    if dev not in current:
        return False, f"Gerät {dev} ist nicht mehr als verifizierter Wechseldatenträger vorhanden."
    now = current[dev]
    if now["size_bytes"] != expected["size_bytes"]:
        return False, (f"Größe von {dev} hat sich geändert "
                        f"({human_size(expected['size_bytes'])} -> "
                        f"{human_size(now['size_bytes'])}). Abbruch zur Sicherheit.")
    if now.get("serial") != expected.get("serial"):
        return False, f"Seriennummer von {dev} stimmt nicht mehr überein. Abbruch zur Sicherheit."
    return True, ""


def _unmount_pass(devpath, log_cb):
    """Ein einzelner Aushäng-Durchlauf über alle aktuell gemounteten
    Kindgeräte. Gibt (alle_ausgehängt: bool) zurück."""
    umount = require_bin("umount")
    ok = True
    for child in device_children(devpath):
        try:
            tree = get_lsblk_tree()
        except Exception:
            tree = []
        mounted = False

        def find_mp(nodes):
            nonlocal mounted
            for n in nodes:
                p = n.get("path") or f"/dev/{n['name']}"
                if p == child and _mountpoints_of(n):
                    mounted = True
                for c in n.get("children", []) or []:
                    find_mp([c])
        find_mp(tree)
        if not mounted:
            continue
        log_cb(f"Hänge {child} aus …")
        r = subprocess.run(privileged([umount, child]), capture_output=True, text=True)
        if r.returncode != 0:
            log_cb(f"Konnte {child} nicht aushängen: {r.stderr.strip()}")
            ok = False
    return ok


def udevadm_settle(timeout_seconds=10):
    """Wartet, bis ausstehende udev-Ereignisse abgearbeitet sind (best
    effort, unprivilegiert). Reduziert Race Conditions zwischen
    Aushängen/Auto-Mount-Reaktionen und nachfolgenden Lesezugriffen."""
    udevadm = shutil.which("udevadm")
    if not udevadm:
        return
    try:
        subprocess.run([udevadm, "settle", f"--timeout={timeout_seconds}"],
                        capture_output=True, timeout=timeout_seconds + 3)
    except Exception:
        pass


def unmount_all_partitions(devpath, log_cb, retries=3, retry_delay=1.5):
    """Hängt alle Partitionen aus, mit Wiederholung: Ubuntus Auto-Mount-
    Dienst (udisksd/GVFS) kann eine gerade ausgehängte Wechseldatenträger-
    Partition unmittelbar danach selbstständig wieder einhängen. Ein
    einmaliger Aushäng-Versuch reicht daher nicht immer - hier wird nach
    kurzer Wartezeit erneut geprüft und bei Bedarf nochmal ausgehängt."""
    for attempt in range(1, retries + 1):
        ok = _unmount_pass(devpath, log_cb)
        time.sleep(retry_delay)
        still_mounted = any(
            _mountpoints_of(n)
            for n in _flatten_tree(get_lsblk_tree_safe())
            if (n.get("path") or f"/dev/{n['name']}") in device_children(devpath)
        )
        if not still_mounted:
            return True
        if attempt < retries:
            log_cb(f"Partition(en) wurden erneut automatisch eingehängt "
                   f"(Auto-Mount-Dienst) — Versuch {attempt + 1}/{retries} …")
    return not still_mounted


def get_lsblk_tree_safe():
    try:
        return get_lsblk_tree()
    except Exception:
        return []


def _flatten_tree(nodes):
    for n in nodes:
        yield n
        yield from _flatten_tree(n.get("children", []) or [])


# --------------------------------------------------------------------------
# Privilegierte Ausführung
# --------------------------------------------------------------------------

def _start_cancel_watcher(proc, cancel_event, cancelled_flag, log_cb, label="Vorgang"):
    """Startet einen Hintergrund-Thread, der bei gesetztem cancel_event den
    übergebenen Prozess SOFORT beendet (im Unterschied zum zeitbasierten
    Stillstands-Watchdog, der erst nach Sekunden reagiert). cancelled_flag
    ist ein gemeinsames dict ({'flag': False}), das der Aufrufer nach
    proc.wait() prüft, um zwischen Abbruch und echtem Fehler zu
    unterscheiden. Gibt None zurück, falls kein cancel_event übergeben wurde
    (Aufrufstellen ohne Abbruch-Unterstützung, z. B. kurze blkid-Aufrufe)."""
    if cancel_event is None:
        return None

    def watch():
        cancel_event.wait()
        cancelled_flag["flag"] = True
        log_cb(f"Abbruch angefordert - beende {label} …")
        try:
            proc.kill()
        except Exception:
            pass

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    return th


def run_pkexec(args, log_cb, progress_cb=None, total_bytes=0, cancel_event=None):
    """Für Aufrufe ohne große Binärdaten auf stdout (z. B. PiShrink)."""
    proc = subprocess.Popen(privileged(args), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    cancelled = {"flag": False}
    _start_cancel_watcher(proc, cancel_event, cancelled, log_cb, label="PiShrink-Lauf")
    buf = ""
    while True:
        ch = proc.stdout.read(1)
        if ch == "" and proc.poll() is not None:
            break
        if ch in ("\r", "\n"):
            line = buf.strip()
            buf = ""
            if not line:
                continue
            if line[:1].isdigit() and " bytes" in line and progress_cb and total_bytes:
                try:
                    copied = int(line.split(" ", 1)[0])
                    progress_cb(min(100.0, copied * 100.0 / total_bytes), line)
                    continue
                except ValueError:
                    pass
            log_cb(line)
        else:
            buf += ch
    proc.wait()
    if cancelled["flag"]:
        raise OperationCancelled("PiShrink-Lauf vom Nutzer abgebrochen.")
    return proc.returncode


def backup_device_to_file(device, dest_final, log_cb, progress_cb, total_bytes,
                           overwrite=False, limit_bytes=None, cancel_event=None):
    """Root LIEST nur (dd if=device, stdout-Pipe). Ein unprivilegierter
    Python-Prozess schreibt die Datei -> Root bekommt keinen beliebigen
    Dateischreibzugriff mehr.

    limit_bytes: falls gesetzt, wird nur bis zu dieser Byte-Anzahl gelesen
    (für die "Kompakt"-Methode, die nur bis zum Ende der letzten Partition
    kopiert statt der ganzen Karte). bs=1M für exakte Zählbarkeit.

    cancel_event: optionales threading.Event - wird es während des Lesens
    gesetzt, bricht der Vorgang sofort ab (dd wird gekillt, die unfertige
    Zieldatei wird gelöscht, OperationCancelled wird ausgelöst)."""
    dd = require_bin("dd")
    dest_final = os.path.abspath(dest_final)

    if os.path.islink(dest_final):
        raise SafetyError("Zielpfad ist ein Symlink - abgelehnt.")
    if os.path.exists(dest_final) and not overwrite:
        raise SafetyError("Zieldatei existiert bereits (keine Überschreib-Bestätigung).")

    dest_tmp = dest_final + ".part"
    if os.path.islink(dest_tmp):
        os.remove(dest_tmp)

    if limit_bytes:
        dd_args = [dd, f"if={device}", "iflag=count_bytes", f"count={limit_bytes}",
                   "bs=4M", "status=progress"]
    else:
        dd_args = [dd, f"if={device}", "bs=4M", "status=progress"]

    proc = subprocess.Popen(privileged(dd_args),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def drain_stderr():
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            if line:
                log_cb(line)

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    # Stillstands-Erkennung: manche USB-Kartenleser blockieren unbegrenzt
    # (beobachtet bei gleichzeitig aktiv gemounteter Partition + Rohgeräte-
    # zugriff). Ohne diese Absicherung hängt das Tool dann ohne jede
    # Fehlermeldung und ohne Abbruchmöglichkeit fest.
    STALL_TIMEOUT_SECONDS = 45
    last_activity = [time.monotonic()]
    stalled = {"flag": False}
    cancelled = {"flag": False}
    stop_watchdog = threading.Event()

    def watchdog():
        # Kurzes Poll-Intervall, damit ein Klick auf "Abbrechen" spürbar
        # sofort reagiert. Die Stillstandserkennung selbst bleibt bei 45s.
        while not stop_watchdog.wait(0.3):
            if cancel_event is not None and cancel_event.is_set():
                log_cb("Abbruch angefordert - beende Lesevorgang …")
                cancelled["flag"] = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            if time.monotonic() - last_activity[0] > STALL_TIMEOUT_SECONDS:
                log_cb(f"Kein Lesefortschritt seit {STALL_TIMEOUT_SECONDS}s — "
                       f"vermutlich blockiert der Kartenleser (z. B. durch eine "
                       f"noch aktiv gemountete Partition). Breche ab.")
                stalled["flag"] = True
                # Gezielter Kill des sudo/dd-Prozesses. KEIN killpg mehr:
                # ohne eigene Prozess-Session (start_new_session wurde
                # entfernt, da es die sudo-Anmeldesitzung gesplittet und
                # doppelte Passwort-Abfragen verursacht hat) würde killpg
                # versehentlich die Prozessgruppe der App selbst treffen.
                # sudo+dd erzeugen in der Praxis keine Enkelprozesse, die
                # die Pipe offenhalten könnten - proc.kill() reicht aus.
                try:
                    proc.kill()
                except Exception:
                    pass
                break

    wd = threading.Thread(target=watchdog, daemon=True)
    wd.start()

    written = 0
    hasher = hashlib.sha256()
    try:
        with open(dest_tmp, "wb") as out:
            while True:
                chunk = proc.stdout.read(4 * 1024 * 1024)
                if not chunk:
                    break
                last_activity[0] = time.monotonic()
                out.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
                if progress_cb and total_bytes:
                    progress_cb(min(100.0, written * 100.0 / total_bytes),
                                f"{written} von {total_bytes} Bytes gelesen")
    finally:
        stop_watchdog.set()
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
        t.join(timeout=5)
        wd.join(timeout=2)

    if cancelled["flag"]:
        try:
            os.remove(dest_tmp)
        except Exception:
            pass
        raise OperationCancelled("Lesevorgang vom Nutzer abgebrochen.")

    if stalled["flag"]:
        try:
            os.remove(dest_tmp)
        except Exception:
            pass
        raise SafetyError(
            f"Lesevorgang nach {STALL_TIMEOUT_SECONDS}s ohne Fortschritt abgebrochen. "
            f"Bitte Partitionen manuell aushängen (umount) und erneut versuchen, "
            f"oder einen anderen Kartenleser/USB-Port probieren.")

    if proc.returncode != 0:
        try:
            os.remove(dest_tmp)
        except Exception:
            pass
        return proc.returncode, None

    os.replace(dest_tmp, dest_final)
    return 0, hasher.hexdigest()


def run_pkexec_stdin_pipe(args, data_source_fn, total_bytes, log_cb, progress_cb,
                           cancel_event=None):
    """Für Restore: root SCHREIBT auf ein Blockgerät, liest Daten von stdin.
    Stdout/Stderr wird in einem separaten Thread parallel gelesen, damit
    volle Pipe-Puffer keinen Deadlock verursachen können.

    cancel_event: optionales threading.Event - wird es während des
    Schreibens gesetzt, wird der Schreibprozess sofort gekillt und
    OperationCancelled ausgelöst (das Zielgerät bleibt dabei in einem
    unvollständigen Zustand zurück - das ist bei einem abgebrochenen
    Restore unvermeidbar und wird geloggt)."""
    proc = subprocess.Popen(privileged(args), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    out_lines = []

    def drain_stdout():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace").strip()
            if line:
                out_lines.append(line)

    reader = threading.Thread(target=drain_stdout, daemon=True)
    reader.start()

    cancelled = {"flag": False}
    _start_cancel_watcher(proc, cancel_event, cancelled, log_cb, label="Schreibvorgang")

    written = 0
    write_error = None
    try:
        for chunk in data_source_fn():
            if cancelled["flag"]:
                break
            try:
                proc.stdin.write(chunk)
            except BrokenPipeError as e:
                write_error = str(e)
                break
            written += len(chunk)
            if progress_cb and total_bytes:
                progress_cb(min(100.0, written * 100.0 / total_bytes),
                            f"{written} von {total_bytes} Bytes geschrieben")
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
    proc.wait()
    reader.join(timeout=5)
    for line in out_lines[-5:]:
        log_cb(line)
    if cancelled["flag"]:
        log_cb("Restore abgebrochen - Zielgerät befindet sich jetzt in einem "
               "unvollständigen, nicht bootfähigen Zustand.")
        raise OperationCancelled("Schreibvorgang vom Nutzer abgebrochen.")
    if write_error:
        log_cb(f"Fehler beim Schreiben: {write_error}")
        return -1 if proc.returncode == 0 else proc.returncode
    return proc.returncode


def parse_parted_bytes(value):
    """Parst parted-Größenangaben robust, auch falls trotz 'unit B' ein
    anderes Suffix (kB/MB/GB/TB) zurückkommt (versions-/lokalisierungs-
    abhängig beobachtet)."""
    value = value.strip()
    for unit, factor in (("TB", 1000 ** 4), ("GB", 1000 ** 3),
                          ("MB", 1000 ** 2), ("kB", 1000), ("B", 1)):
        if value.endswith(unit):
            number = value[:-len(unit)]
            try:
                return int(float(number) * factor)
            except ValueError:
                break
    raise ValueError(f"Unbekannte parted-Größe: {value}")


BLKID_TIMEOUT_SECONDS = 10


def run_blkid(args, timeout=BLKID_TIMEOUT_SECONDS):
    """Führt 'blkid' mit Timeout aus - erst unprivilegiert, bei Fehlschlag
    mit sudo -A. blkid sondiert Signaturen direkt und öffnet das Gerät nicht
    exklusiv (anders als 'parted'), sollte daher praktisch nie hängen -
    der Timeout bleibt trotzdem als Sicherheitsnetz bestehen.
    Gibt subprocess.run()-Ergebnis zurück, 'timeout' bei Zeitüberschreitung,
    oder None wenn blkid fehlt."""
    blkid = shutil.which("blkid")
    if not blkid:
        return None
    try:
        r = subprocess.run([blkid] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout"
    if r.returncode != 0:
        try:
            r = subprocess.run(privileged([blkid] + args), capture_output=True,
                                text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout"
    return r


def _parse_blkid_export(output):
    """Parst 'blkid -p -o export'-Ausgabe (KEY=VALUE-Zeilen) in ein dict."""
    result = {}
    for line in output.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def get_table_type(device):
    """Liest den Partitionstabellentyp direkt per blkid-Signatursondierung.
    Gibt (table_type, fehlergrund) zurück - table_type ist z. B. 'dos'
    (MBR!) oder 'gpt'. Wichtig: blkid nennt MBR 'dos', NICHT 'msdos' wie
    parted - mit echtem Loop-Device-Test verifiziert, nicht nur angenommen."""
    r = run_blkid(["-p", "-o", "export", device])
    if r is None:
        return None, "blkid nicht gefunden"
    if r == "timeout":
        return None, f"blkid antwortete nicht innerhalb von {BLKID_TIMEOUT_SECONDS}s"
    if r.returncode != 0:
        return None, f"blkid fehlgeschlagen: {r.stderr.strip()}"
    info = _parse_blkid_export(r.stdout)
    table = info.get("PTTYPE")
    return normalize_table_type(table), None

def normalize_table_type(t):
    if not t:
        return t
    t = t.lower()
    if t in ("dos", "msdos"):
        return "msdos"
        return t

def get_partitions_sorted(device):
    """Liste aller Partitionen mit Offset/Größe/Dateisystem, per blkid
    direkt sondiert (PART_ENTRY_OFFSET/PART_ENTRY_SIZE sind bei blkid immer
    in 512-Byte-Einheiten, unabhängig von der echten Sektorgröße - mit
    echtem Loop-Device-Test verifiziert) und nach physischer Position auf
    der Platte sortiert (Reihenfolge in lsblk-children ist NICHT
    garantiert nach Position sortiert).

    Gibt eine Liste von dicts zurück: {'path', 'fstype', 'offset_bytes',
    'size_bytes'}, oder None bei Fehler/Timeout (Aufrufer muss das wie
    ein unbekanntes Layout behandeln)."""
    children = [c for c in device_children(device)]
    if not children:
        return None
    partitions = []
    for part_path in children:
        r = run_blkid(["-p", "-o", "export", part_path])
        if r is None or r == "timeout" or r.returncode != 0:
            return None
        info = _parse_blkid_export(r.stdout)
        try:
            offset = int(info.get("PART_ENTRY_OFFSET", "0")) * 512
            size = int(info.get("PART_ENTRY_SIZE", "0")) * 512
        except ValueError:
            return None
        if size <= 0:
            continue
        partitions.append({
            "path": part_path,
            "fstype": (info.get("TYPE") or "").lower(),
            "offset_bytes": offset,
            "size_bytes": size,
        })
    partitions.sort(key=lambda p: p["offset_bytes"])
    return partitions


def check_rpi_compatible_layout(device):
    """Prüft, ob das Layout zu PiShrink passt, BEVOR PiShrink mit Root-Rechten
    läuft: MBR-Partitionstabelle und letzte Partition mit ext2/3/4-Dateisystem
    (das ist alles, was PiShrink selbst voraussetzt). Verhindert, dass der
    Modus 'Raspberry Pi OS' unkontrolliert auf jedem beliebigen Image
    (OpenWrt, HAOS, ...) angewendet wird, nur weil PiShrink zufällig
    installiert ist.

    Gibt (ok: bool, grund: str) zurück."""
    table_type, err = get_table_type(device)
    if err:
        return False, f"Partitionstabelle konnte nicht gelesen werden: {err}"
    if table_type != "msdos":
        return False, (f"Partitionstabelle ist {table_type!r}, PiShrink benötigt "
                        f"eine MBR-Tabelle ('dos').")

    partitions = get_partitions_sorted(device)
    if partitions is None:
        return False, "Partitionen konnten nicht einzeln gelesen werden."
    if len(partitions) < 2:
        return False, "Es wurden weniger als zwei Partitionen gefunden."

    last_fstype = partitions[-1]["fstype"]
    if last_fstype not in ("ext2", "ext3", "ext4"):
        return False, (f"Letzte Partition hat Dateisystem {last_fstype!r}, "
                        f"PiShrink benötigt ext2/ext3/ext4.")

    return True, f"Layout kompatibel (MBR, letzte Partition {last_fstype})."


def compact_copy_size(device, device_size_bytes):
    """Ermittelt über blkid das Ende der letzten Partition + Sicherheits-
    puffer sowie den Partitionstabellentyp. Funktioniert unabhängig vom
    Dateisystem (ext4, squashfs, f2fs, btrfs, ...), da nur Partitions-
    Metadaten gelesen werden, nicht der Dateisysteminhalt.

    Gibt (byte_anzahl, table_type) zurück. table_type ist z. B. 'dos'
    (MBR) oder 'gpt' - bei 'gpt' MUSS der Aufrufer den Kompakt-Modus
    ablehnen, da sonst das sekundäre GPT-Backup-Header am Geräteende
    fehlen würde. table_type ist 'timeout' oder None bei Problemen -
    auch das MUSS der Aufrufer wie ein unbekanntes Layout behandeln,
    nicht wie eine sichere volle Kopie.
    Fällt bei sonstigen Problemen sicher auf die volle Gerätegröße zurück."""
    table_type, err = get_table_type(device)
    if err:
        return device_size_bytes, ("timeout" if "nicht innerhalb" in err else None)

    partitions = get_partitions_sorted(device)
    if not partitions:
        return device_size_bytes, table_type

    last = partitions[-1]
    last_end = last["offset_bytes"] + last["size_bytes"]
    buffer_bytes = 8 * 1024 * 1024  # 8 MB Sicherheitspuffer für Rundung/Alignment
    result = min(last_end + buffer_bytes, device_size_bytes)
    return result, table_type


def gzip_compress_file(src_path, dest_final, log_cb, progress_cb=None, cancel_event=None):
    """Komprimiert eine lokale Datei unprivilegiert nach dest_final (atomar
    über *.part -> rename), dann wird src_path gelöscht. Gibt den SHA-256
    der resultierenden (komprimierten) Datei zurück.

    cancel_event: optionales threading.Event - wird während der Komprimierung
    geprüft (reiner Python-Loop ohne Subprozess, daher genügt ein einfacher
    Check pro Chunk statt eines Kill-Mechanismus)."""
    dest_final = os.path.abspath(dest_final)
    dest_tmp = dest_final + ".part"
    total = os.path.getsize(src_path)
    written = 0
    hasher = hashlib.sha256()
    try:
        with open(src_path, "rb") as fin, gzip.open(dest_tmp, "wb") as fout:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    log_cb("Abbruch angefordert - beende Komprimierung …")
                    raise OperationCancelled("Komprimierung vom Nutzer abgebrochen.")
                chunk = fin.read(4 * 1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
                written += len(chunk)
                if progress_cb and total:
                    progress_cb(min(100.0, written * 100.0 / total),
                                f"Komprimiere: {written} von {total} Bytes")
        # Hash über die tatsächlich geschriebene (komprimierte) Datei bilden
        with open(dest_tmp, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                hasher.update(chunk)
    except Exception:
        try:
            os.remove(dest_tmp)
        except Exception:
            pass
        raise
    os.replace(dest_tmp, dest_final)
    try:
        os.remove(src_path)
    except Exception:
        pass
    return hasher.hexdigest()


def write_sha256_sidecar(dest_final, digest):
    """Schreibt eine <dest>.sha256-Datei im Standardformat für
    'sha256sum -c', atomar über *.part -> rename, damit bei einem Abbruch
    keine unvollständige Sidecar-Datei zurückbleibt."""
    if not digest:
        return
    sidecar = dest_final + ".sha256"
    sidecar_tmp = sidecar + ".part"
    with open(sidecar_tmp, "w") as f:
        f.write(f"{digest}  {os.path.basename(dest_final)}\n")
    os.replace(sidecar_tmp, sidecar)


def full_gzip_size(path, progress_cb=None, max_size=None):
    """Vollständiger Validierungs-Durchlauf: entpackt einmal komplett und
    zählt die echte Größe, statt sich auf den unzuverlässigen gzip-Trailer
    zu verlassen (der bei >4 GiB falsch sein kann).

    max_size: falls gesetzt, wird sofort abgebrochen sobald mehr als
    max_size Bytes entpackt wurden, statt ein absichtlich oder versehentlich
    stark aufgeblähtes Archiv vollständig zu Ende zu dekomprimieren."""
    total = 0
    with gzip.open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_size and total > max_size:
                raise SafetyError(
                    f"Entpacktes Image überschreitet bereits nach {human_size(total)} "
                    f"die Zielgröße von {human_size(max_size)} — Abbruch.")
            if progress_cb:
                progress_cb(total)
    return total



# --------------------------------------------------------------------------
# GUI (CustomTkinter — dunkles Theme, abgerundete Ecken, moderne Akzentfarbe)
# --------------------------------------------------------------------------

ctk.set_appearance_mode("dark")

# Eigenes Navy-Theme (durchgängig über Hauptfenster, alle CTkToplevel-Dialoge
# und den Dateidialog) statt CustomTkinters Standard-Grau. Struktur nach
# Vorlage von customtkinter/assets/themes/blue.json, Werte an das Navy-Blau
# des Askpass-Dialogs angeglichen (#1a1a2e Hintergrund, #3a7ebf/#5b9bd5 Akzent).
# Fallback auf "blue", falls die Theme-Datei bei einer Kopie des Skripts
# ohne Begleitdatei fehlt.
_THEME_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "aurapi_navy_theme.json")
ctk.set_default_color_theme(_THEME_PATH if os.path.isfile(_THEME_PATH) else "blue")

# Fester linker Abstand für den Icon+Button-Block auf Backup- und Restore-Tab
# (siehe _build_backup_tab / _build_restore_tab) - beide verwenden exakt
# denselben Wert, damit Icon und Start-Button beim Tab-Wechsel nicht seitlich
# springen.
_ACTION_FRAME_LEFT_PADX = 180

# Marken-Assets (Wortmarke + Icon), direkt als Base64 im Code eingebettet.
# Bewusst KEINE externen Bilddateien: eine einzelne .py-Datei ohne separaten
# "assets"-Ordner kann nicht durch einen fehlenden/falsch abgelegten Ordner
# kaputtgehen (das war die Ursache, als die Grafiken zuerst nicht geladen
# wurden). SVG selbst kann tkinter/CustomTkinter nicht direkt rendern - die
# Original-Vektordateien (aurapi_wordmark.svg, aurapi_icon_still.svg) wurden
# daher einmalig nach PNG konvertiert und hier als Base64-Text eingebettet.
#
# Für das Icon ist bewusst ein "still"-Bild (unanimiert, aktueller Zustand)
# und ein Ansatzpunkt für eine spätere "animated"-Sequenz vorgesehen: die
# animierte Lotus-Sequenz (GIF oder animiertes SVG, spielt während des
# Backup-Vorgangs) kann später denselben Container per
# set_icon_animated_gif() bespielen, ohne das Layout ändern zu müssen.

def _decode_embedded_image(b64_text):
    """Dekodiert eine eingebettete Base64-Bild-Zeichenkette (PNG oder GIF) zu
    einem PIL-Image. Für Mehrbild-GIFs gibt Image.open das erste Frame zurück
    -- für Multi-Frame-Zugriff siehe ImageSequence.Iterator in
    set_icon_animated_gif()."""
    return Image.open(io.BytesIO(base64.b64decode(b64_text)))


_WORDMARK_PNG_B64 = """
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
XA18proOSZIkSVpVNhgkaYJFxO0R8cfANsAxwP3FJWl67gTeDWwbET50lPrhffheLUmSJGlM2GCQpB6IiCsj4k3ArsBXquvRI7oT+Dtgi4h4V0TcU12Q
pDYi4jLgi9V1SJIkSdKqsMEgST0SERdExN50B0EfCzjHf7TcASykayz8eUT8srogSSX+troASZIkSVoVNhgkqYci4pKIeA3dGQ0fB+4uLqnvfgL8MbBZ
RLzFxoLUbxHxLeDs6jokSZIk6ZHYYJCkHouIH0bEG4DNgD+he9CtNu4HTgX2oztj4R8i4vbimiSNjoXVBUiSJEnSI7HBIEkiIm6JiA8C2wJ7041PuqO2
qon1A+CtwOYRsX9EnBYRHugqaTkR8SXg4uo6JEmSJOnhzKsuQJI0OgYPur8CfCUz1wYOBBYA+wBrVtY25i4HTgKOi4jzq4uRNDb+nq7hK0mSJEkjyQaD
JGmlIuIe4DjguMycD7wQ2Hfw2qKwtHGwFLgAOAX4QkRcWlyPpPH0GeBdwNbFdUiSJEnSStlgkCQ9ooi4i+5h+SkAmflUuobDc4HdgC3rqhsJ9wOXAl8f
vM6KiNtqS9I0LAZuqS5C3FtdwKiJiKWZ+U/Ah6prkSRJkqSVieoCJEnjLzM3oWs0PBt4OrAj3cHRkyiBK4DzB68LgAsjwjMrJEmSJElSr9hgkCQNRWY+
BngaXbPhN+h2OSx7rV9Y2qq6BbgS+ClwGd3hzD8ELhvs6JAkSZIkSeo1GwySpOYycyNgc2AT4HHAxoPX44DHA48B5g9e6w9eq88y9hbg7sHrNuAXwE1T
XtcDN9DtTrjSHQmSJEmSJEkPzwaDJGksZOZawNqDf3wUXfNhmdXpDlaeatlM/XsHB1ZLkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJ
kiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJ
kiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJ
kiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJ
kiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJ
kiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJ
kiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJ
kiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJ
kiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkiRJkqTR8f8BLqQarkgV2VMAAAAASUVORK5CYII=
""".strip()

_ICON_STILL_B64 = """
R0lGODlhOAQ4BIABAP///////yH/C1hNUCBEYXRhWE1QPD94cGFja2V0IGJlZ2luPSLvu78iIGlkPSJXNU0wTXBDZWhpSHpyZVN6TlRjemtjOWQiPz4g
PHg6eG1wbWV0YSB4bWxuczp4PSJhZG9iZTpuczptZXRhLyIgeDp4bXB0az0iQWRvYmUgWE1QIENvcmUgOS4xLWMwMDEgNzkuMTQ2Mjg5OTc3NywgMjAy
My8wNi8yNS0yMzo1NzoxNCAgICAgICAgIj4gPHJkZjpSREYgeG1sbnM6cmRmPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5LzAyLzIyLXJkZi1zeW50YXgt
bnMjIj4gPHJkZjpEZXNjcmlwdGlvbiByZGY6YWJvdXQ9IiIgeG1sbnM6eG1wPSJodHRwOi8vbnMuYWRvYmUuY29tL3hhcC8xLjAvIiB4bWxuczp4bXBN
TT0iaHR0cDovL25zLmFkb2JlLmNvbS94YXAvMS4wL21tLyIgeG1sbnM6c3RSZWY9Imh0dHA6Ly9ucy5hZG9iZS5jb20veGFwLzEuMC9zVHlwZS9SZXNv
dXJjZVJlZiMiIHhtcDpDcmVhdG9yVG9vbD0iQWRvYmUgUGhvdG9zaG9wIDI1LjIgKE1hY2ludG9zaCkiIHhtcE1NOkluc3RhbmNlSUQ9InhtcC5paWQ6
NDFBNzgxODdBMjBDMTFGMTkwQkI4MTk3QkRFRUNFQ0EiIHhtcE1NOkRvY3VtZW50SUQ9InhtcC5kaWQ6NDFBNzgxODhBMjBDMTFGMTkwQkI4MTk3QkRF
RUNFQ0EiPiA8eG1wTU06RGVyaXZlZEZyb20gc3RSZWY6aW5zdGFuY2VJRD0ieG1wLmlpZDo0MUE3ODE4NUEyMEMxMUYxOTBCQjgxOTdCREVFQ0VDQSIg
c3RSZWY6ZG9jdW1lbnRJRD0ieG1wLmRpZDo0MUE3ODE4NkEyMEMxMUYxOTBCQjgxOTdCREVFQ0VDQSIvPiA8L3JkZjpEZXNjcmlwdGlvbj4gPC9yZGY6
UkRGPiA8L3g6eG1wbWV0YT4gPD94cGFja2V0IGVuZD0iciI/PgH//v38+/r5+Pf29fTz8vHw7+7t7Ovq6ejn5uXk4+Lh4N/e3dzb2tnY19bV1NPS0dDP
zs3My8rJyMfGxcTDwsHAv769vLu6ubi3trW0s7KxsK+urayrqqmop6alpKOioaCfnp2cm5qZmJeWlZSTkpGQj46NjIuKiYiHhoWEg4KBgH9+fXx7enl4
d3Z1dHNycXBvbm1sa2ppaGdmZWRjYmFgX15dXFtaWVhXVlVUU1JRUE9OTUxLSklIR0ZFRENCQUA/Pj08Ozo5ODc2NTQzMjEwLy4tLCsqKSgnJiUkIyIh
IB8eHRwbGhkYFxYVFBMSERAPDg0MCwoJCAcGBQQDAgEAACH5BAEAAAEALAAAAAA4BDgEAAL/jI+py+0Po5y02ouz3rz7D4biSJbmiabqyrbuC8fyTNf2
jef6zvf+DwwKh8Si8YhMKpfMpvMJjUqn1CDAcA1kt9iu1sv9isNksHl8LqPX6nb6zYa74/S5XY6v5+/6Pv+/F+gnCDhoWIhIqHi4mMj46BjZOAlJKVmJ
ealpyZnZqVYlcgVAWmp6ipqqusra6voKGys7S1tre4ubq7vL2+v7CxwsPExcbHyMnKy8zNzcG3ox6jxNXW19jZ2tvc3d7f0NHi4+Xgz9QI6err7O3u7+
Dh8vP08fbH5Qn6+/z9/v/w8woMCBtEIRPIgwocKFDBs6fEhPCsSJFCtavIgxo0aB/042evwIMqTIkSRLrlLySwspldJasnxZCubKmDRn2nRZE+dNmTp7
8vyZE+jOoESHGvVZFOlRoUqbMn2aFOrSqFSnWnVaFetVqVq7cv2aFezWsGTHmvVaFu1ZsWrbsn2bFu7auHTn2m37rEiue3z7+v0LOLDgwYSh4BpSq7Di
xYwbO34MOfLfxD8KSr6MObPmzZw7e45geYesz6RLmz6NOrVqIKNztF4NO7bs2bRr035NI5bt3bx7+/4NHJru3K+CGz+OPLny5SpgzSjOPLr06dSrK4f+
Arv17dy7e/8uWfsKV+DLmz+PPv0T8izYq38PP778+eNb1WdFP7/+/fz7L/9wb4J9/g1IYIEGdidgCQkeyGCDDj54G34KSghhhRZeiGFkC4JAYYYefghi
iFNs6EGHIp6IYooq8mAiBySuCGOMMs4YwosZnERjjjruyCMELd6IY49CDkkkjD9acGSRSi7JJINJThBkk1JOSeV+UUZzZZVabsklgllS8GWXYo5JZnBh
gqZKmWquyaZtZzrwZJtyzkknZHH+l2adeu7J52JvMvBnn4IOSugSgSZwaKGKLsqoaHlKkGijkk5KaXOP+nhppZpuymmAmTYQaaeijkoqnqlAemqpqq7K
qqmnoJlqq7LOKmqgodKKa650fqoAr7r+CmydviIwbLDGHitmsSz/oYJss84mW6yyz05LLY/RxlptttrueC2z234LrpHYIiptuNCuZC6fYZab7pbetivs
uPjIC++Y7Narpa/34rvknfxWqS+9/wJ868BKBvyuwe76q3C/Au/bMLc2Riwlwq9SPKVzGOdLb8Ebiwvgx01+CbHIKuJm8sEPJ5yykCi3TGTHJcMMYmg0
u8zrzDdnaPPOErM878U+04ijx0PzLLPAR59YtM5LX5iz0k9/GKXRUz9osSlXn3yp1Vs7mbTQX4eYpddjFxg10GdD/anZa/eXdUxvI72y1HNjHbbWd2O4
rtN760ey3X/796bbg8dXt9iHH1i434vDl7baj8MtreGT/5sXuOKXE16545ufl7jen1MuOBaej+5d5JqjPl+ilrNuXeaiw05fqK/TLl3c6OIun+2n8z5d
3rMDr96ttxOPnOzDI495ycczb2bou0MPeumuLk999as/n32E1vf6e/e/KS+3+KnrzL353q++bPnqb+d1+u/Dpjr2BjGbxfw1xB++/u19jxjhTe9+IXuW
+0rjNvnd44D8Gg4TdJe/KvQMWZLbTAL7Nxn2mWuCRqjfACVyC2dhkC+WUyAVRiirw6BEgBGMgi6aZcIR/S6GIAQgDPeCBAiecBcUpKFhbHgOFErQh5vi
RQ5ZuEMjBquAl7kdEQ01MWqlpIMebGETgLFEJv9qCIiYquBjtFgtLFKRfUKMgcaCxrBRnbGJI3ziEcFowALCkUUstCIUG+dGNjkwPG0sY0fWqK0oRtFR
AnSh6/JYplmwkYuw8mJhXibFQ/pxBNL74AoBiEgyKZKPjOyiIwezyW1FKo37q+MPMZnJc0HykX5MZQ8oky3jTbJGHjzlJ8HXyUbBsjEKdCUhQxlG583y
Azr84y2vx8BV5WmQgJnlMAPYtGP+Cn25dIEpjanBIEpTU0EipXCc+UzWUCicijIaOTdAPku+MZtwOqe9sMVMEoKzmtiEpzsHRc1tPqeS63EaPQuVqXh+
85+NZOcCTXTPPvmToJbaXkIpqU9iPXT/YUAT6BAZqs2I2hKJwdSo6TDqKY4+cKEG5VQ67SdPj2JgosQ5aSzzWVIb6G6jySwoSmulNIsaEqQZjWkNi9nR
m/a0pr/Eni85xEiWEsyL3uynSoH01CRM7KhzMidPQ+rQqOIApkLtlLJ0ekV3UvV/f1LqrpJ6VRIAdaRAHOvIvtrUO2oVSWa9T+fmqkZhphWiaqtrifSK
V4WWDqxy7apa/XqCeCJWjxBzK6r6utfs7MuxTJJkZLeaUMoe1rKGneZkFztUdYJWAwyLqy6/R9gjpHK0ohglazV5qNSaUaRO7dZrY4ZKzdLysqFVZ231
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
""".strip()

# Animierte Lotus-Sequenz (spielt waehrend eines laufenden Backups/Restores),
# ebenfalls direkt eingebettet statt als externe Datei - aus demselben Grund
# wie Wortmarke/Still-Icon: keine externe Datei, die fehlen oder falsch
# abgelegt sein koennte. GIF mit 42 Frames, wird beim ersten Gebrauch einmalig
# dekodiert und als CTkImage-Liste gecacht (siehe set_icon_animated_gif()).
_ICON_ANIM_B64 = """
R0lGODlhOAQ4BIABAP///////yH/C05FVFNDQVBFMi4wAwEAAAAh/wtYTVAgRGF0YVhNUDw/eHBhY2tldCBiZWdpbj0i77u/IiBpZD0iVzVNME1wQ2Vo
aUh6cmVTek5UY3prYzlkIj8+IDx4OnhtcG1ldGEgeG1sbnM6eD0iYWRvYmU6bnM6bWV0YS8iIHg6eG1wdGs9IkFkb2JlIFhNUCBDb3JlIDkuMS1jMDAx
IDc5LjE0NjI4OTk3NzcsIDIwMjMvMDYvMjUtMjM6NTc6MTQgICAgICAgICI+IDxyZGY6UkRGIHhtbG5zOnJkZj0iaHR0cDovL3d3dy53My5vcmcvMTk5
OS8wMi8yMi1yZGYtc3ludGF4LW5zIyI+IDxyZGY6RGVzY3JpcHRpb24gcmRmOmFib3V0PSIiIHhtbG5zOnhtcD0iaHR0cDovL25zLmFkb2JlLmNvbS94
YXAvMS4wLyIgeG1sbnM6eG1wTU09Imh0dHA6Ly9ucy5hZG9iZS5jb20veGFwLzEuMC9tbS8iIHhtbG5zOnN0UmVmPSJodHRwOi8vbnMuYWRvYmUuY29t
L3hhcC8xLjAvc1R5cGUvUmVzb3VyY2VSZWYjIiB4bXA6Q3JlYXRvclRvb2w9IkFkb2JlIFBob3Rvc2hvcCAyNS4yIChNYWNpbnRvc2gpIiB4bXBNTTpJ
bnN0YW5jZUlEPSJ4bXAuaWlkOjQxQTc4MTgzQTIwQzExRjE5MEJCODE5N0JERUVDRUNBIiB4bXBNTTpEb2N1bWVudElEPSJ4bXAuZGlkOjQxQTc4MTg0
QTIwQzExRjE5MEJCODE5N0JERUVDRUNBIj4gPHhtcE1NOkRlcml2ZWRGcm9tIHN0UmVmOmluc3RhbmNlSUQ9InhtcC5paWQ6NDFBNzgxODFBMjBDMTFG
MTkwQkI4MTk3QkRFRUNFQ0EiIHN0UmVmOmRvY3VtZW50SUQ9InhtcC5kaWQ6NDFBNzgxODJBMjBDMTFGMTkwQkI4MTk3QkRFRUNFQ0EiLz4gPC9yZGY6
RGVzY3JpcHRpb24+IDwvcmRmOlJERj4gPC94OnhtcG1ldGE+IDw/eHBhY2tldCBlbmQ9InIiPz4B//79/Pv6+fj39vX08/Lx8O/u7ezr6uno5+bl5OPi
4eDf3t3c29rZ2NfW1dTT0tHQz87NzMvKycjHxsXEw8LBwL++vby7urm4t7a1tLOysbCvrq2sq6qpqKempaSjoqGgn56dnJuamZiXlpWUk5KRkI+OjYyL
iomIh4aFhIOCgYB/fn18e3p5eHd2dXRzcnFwb25tbGtqaWhnZmVkY2JhYF9eXVxbWllYV1ZVVFNSUVBPTk1MS0pJSEdGRURDQkFAPz49PDs6OTg3NjU0
MzIxMC8uLSwrKikoJyYlJCMiISAfHh0cGxoZGBcWFRQTEhEQDw4NDAsKCQgHBgUEAwIBAAAh+QQFCAABACwAAAAAOAQ4BAAC/4yPqcvtD6OctNqLs968
+w+G4kiW5omm6sq27gvH8kzX9o3n+s73/g8MCofEovGITCqXzKbzCY1Kp9QgwHANZLfYrtbL/YrDZLB5fC6j1+p2+s2Gu+P0uV2Or+fv+j7/vxfoJwg4
aFiISKh4uJjI+OgY2TgJSSlZiXmpacmZ2alWJXIFQFpqeoqaqrrK2ur6ChsrO0tba3uLm6u7y9vr+wscLDxMXGx8jJysvMzc3Bt6Meo8TV1tfY2drb3N
3e39DR4uPl4M/UCOnq6+zt7u/g4fLz9PH2x+UJ+vv8/f7/8PMKDAgbRCETyIMKHChQwbOnxITwrEiRQrWryIMaNGgf9ONnr8CDKkyJEkS65S8ksLKZXS
WrJ8WQrmypg0Z9p0WRPnTZk6e/L8mRPozqBEhxr1WRTpUaFKmzJ9mhTq0qhUp1p1WhXrValau3L9mhXs1rBkx5r1WhbtWbFq27J9mxbu2rh059pt+6xI
rnt8+/r9Cziw4MGEoeAaUquw4sWMGzt+DDny38Q/Ckq+jDmz5s2cO3uOYHmHrM+kS5s+jTq1aiCjc7ReDTu27Nm0a9N+TSOW7d28e/v+DRya7tyvghs/
jjy58uUqYM0ozjy69OnUqyuH/gK79e3cu3v/Lln7Clfgy5s/jz79E/Is2Kt/Dz++/PnjW9VnRT+//v38+y//cG+Cff4NSGCBBnYnYAkJHshggw4+eBt+
CkoIYYUWXohhZAuCQGGGHn4IYohTbOhBhyKeiGKKKvJgIgckrghjjDLOGMKLGZxEY4467sgjBC3eiGOPQg5JJIw/WnBkkUouySSDSU4QZJNSTknlflFG
c2WVWm7JJYJZUvBll2KOSWZwYYKmSplqrsmmbWc68GSbcs5JJ2Rx/pdmnXruyedibzLwZ5+CDkroEoEmcGihii7KqGh5SpBoo5JOSmlzj/p4aaWabspp
gJk2EGmnoo5KKp6pQHpqqaquyqqpp6CZaquyzipqoKHSimuudH6qAK+6/gpsnb4iMGywxh4rZrEs/6GCbLPOJlusss9OSy2P0cZabbba7ngts9t+C66R
2CIqbbjQrmQun2GWm+6W3rYr7Lj4yAvvmOzWq6Wv9+K75J38Vqkvvf8CfOvASgb8rsHu+qtwvwLv2zC3NkYsJcKvUjylcxjnS2/BG4sL4MdNfgmxyCri
ZvLBDyecspAot0xkxyXDDGJoNLvM68w3Z2jzzhKzPO/FPtOIo8dD8yyzwEefWLTOS1+Ys9JPfxil0VM/aLEpV598qdVbO5m00F+HmKXXYxcYNdBnQ/2p
2Wv3l3VMbyO9stRzYx221ndjuK7Te+tHst1/+/em24PHV7fYhx9YuN+Lw5e22o/DLa3hk/+bF7jilxNeueObn5e43p9TLjgWno/uXeSaoz5fopazbl3m
osNOX6iv0y5d3OjiLp/tp/M+Xd6zA6/erbcTj5zswyOPecnHM29m6LtDD3rpri5PffWrP599hNb3+nv3vykvt/ip68y9+d6vvmz56m/ndfrvw6Y69gYx
m8X8NcQfvv7tfY8Y4U3vfiF7lvtK4zb53eOA/BoOE3SXvyr0DFmS20wC+zcZ9plrgkao3wAlcgtnYZAvllMgFUYoq8OgRIARjIIummXCEf0uhiAEIAz3
ggQInnAXFKShYWx4DhRK0Ieb4kUOWbhDIwargJe5HRENNTFqpaSDHmxhE4CxRCb/agiImKrgY7RYLSxSkX1CjIHGgsawUZ2xiSN84hHBaMACwpFFLLQi
FBvnRjY5MDxtLGNH1qitKEbRUQJ0oevyWKZZsJGLsPJiYV4mxUP6cQTS++AKAYhIMimSj4zsoiMHs8ltRSqN+6vjDzGZyXNB8pF+TGUPKJMt402yRh48
5SfB18lGwbIxCnQlIUMZRufN8gM6/OMtr8fAVYXQMbMcZgCbdsxfoS+XLjClMTUYxGhqSoV+aqYzWUOhbyrKaOLcAPks+UZswqmc9uImK6nZSHWSsEO+
1NM0tfmcSq7HafAs1AvfiU8g9fOOmWKnoPg5UEttz6Ak6GQ9qcRDwvjw/6Fm/BFD1eXQi1bgnHY8gtUS2qe8CEacGiUOR2N5T3neQHe2tJ8nVbpNkWYQ
pC91qXCOVFJ7BhSNNnWNNa/IyJxCVKZ+YadQq5kkiraJnDQ9QRX3udP2JbNUYuxLHpWqUGUdVU4p7SkOiglUIGLVYVO0qlG3moJBShWdxyrYWNepNrS6
SJhNjVdVF1jXeHr1mqWT65pkmdcJxTWw2dnXW1VmD7wSFq5R1QvD/PpXw0IWVJXsaDqPSUpKEUOxjZ1rZ5/5vcMm8lBq/SoSDYnHyRapHAT8rDlVWyLA
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
1xnPe9Zzn/MMZzMHms2CdvOgDV1oRBNa0YdedKKtvEtIR1rSk6Z0pS196dMUAAAh+QQFCAABACwAAAAAAQABAAACAkwBACH5BAUIAAEALAAAAAABAAEA
AAICTAEAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQAh+QQFCAABACwAAAAAAQABAAACAkwBACH5BAkIAAEALBMBZgDQAKwBAAL/jI+py+0Po5y02ouz3rz7
D4biSJbmiabqyrbuC8fyTNf2jef6zvf+DwwKh8Si8YhMKpfMpvMJjUqn1Kr1is1qt9yu9wsOi8fksvmMTqvX7Lb7DY/L5/S6/Y7P6/f8vv8PGCg4SFho
eIiYqLjI2Oj4CBkpOUlZaXmJmam5ydnp+QkaKjpKWmp6ipqqusra6voKGys7S1tre4ubq7vL2+v7CxwsPExcbHyMnKy8zNzs/AwdLT1NXW19jZ2tvc3d
7f0NHi4+Tl5ufo6err7O3u7+Dh8vP09fb3+Pn6+/z9/v/w8woMCBBAsaPIgwocKFDBs6fAgxosSJFCtavIgxo8aNohw7evwIMqTIkSRLmjyJMqXKlSxb
unwJM6bMmTRr2ryJM6fOnTx7+vwJNKjQoUSLGj2KNKnSpUybOn0KNarUqVSrWr2KNavWrVy7ev0KNqzYsWTLmj2LNq3atWzbun0LN67cuXTr2r2LN6/e
vXz7+v0LOLDgwYQLGz6MOLHixYwbO34MObLkyZQrW76MObPmzZw7e/4MOrTo0aRLmz6NOnWxAgAh+QQFCAABACwAAAAAOAQ4BAAC/4yPqcvtD6OctNqL
s968+w+G4kiW5omm6sq27gvH8kzX9o3n+s73/g8MCofEovGITCqXzKbzCY1Kp9Sq9YrNarfcrvcLDovH5LL5jE6r1+y2+w2Py+f0uv2Oz+v3/L7/Dxgo
OEhYaHiImKi4yNjo+AgZKTlJWWl5iZmpucnZ6fkJGio6SlpqeoqaqrrK2ur6ChsrO0tba3uLm6u7y9vr+wscLDxMXGx8jJysvMzc7PwMHS09TV1tfY2d
rb3N3e39DR4uPk5ebn6Onq6+zt7u/g4fLz9PX29/j5+vv8/f7/8PMKDAgQQLGjyIMKHChQwbOnwIMaLEiRQrWryIMaPGjf8cO3r8CDKkyJEkS5o8iTKl
ypUsW7p8CTOmzJk0a9q8iTOnzp08e/r8CTSo0KFEixo9ijSp0qVMmzp9CjWq1KlUq1q9ijWr1q1cu3r9Cjas2LFky5o9izat2rVs27p9Czeu3Ll069q9
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
tWmLxIWbuVlbu0mDruWbv3lawSmckEWcxQlYxwmLyamcyJlWzbmczwmdzqlU06mKcGWd0Vmd2UmdK8Wd17md39mdE5WdNhiXOlWe5rmWM5We6gmD7Dmd
6ZeVI2WdLqiS9P8Zn9CYBvgJnSbYYgOVn/4JBOTZn9U4fABaoAZqlPsUoAqKfPNUn9p4ntzUng5aH+tUoRqYIOT0nT+2mcwknpjFmNQUoh76mMMkngdp
oT2pSymqohrKkC3qoiJqmDI6oyY6op/kogaon/upozv6k/bpgD8KpC8qoO/TSEXKo0K6kZakpEEqn3bgpE8KpTIoeHpEpUsapXVASFmqpVvalF3kpV9q
pWUppmNqpDAakGeEplVapl95pm2apj06pEkkp2T6pngQp3JqWx86Q3d6dmC6oAIJqHiap1zqQ4UaqMvIlX+qqIbqnvP5HsjzqG56qFYpZX3Kp5pao95C
o09KqZ7/qTKfSqWh6qcWQ6qlyql6yWcrmpqmWpntJ6E3mqpmukOzSqs4+p4sWakiyaTKeJKRCqTfaAlq2qt6SqfHCn/JqqzryajNWqzMCq1oKajTeqKX
aq2kKazZGq3Yyq2c9qzfSpriuoPkWq7mqm7omq7qCnrsKmvu+q7wKq/zSq/1aq/3iq/5qq/7yq/96q//CrABK7ADS7AFa7AHi7AJq7ALy7AN67APC7ER
K7ETS7EVa7EXi7EZq7Eby7Ed67EfC7IhK7IjS7Ila7Ini7Ipq7Iry7It67IvC7MxK7MzS7M1a7M3i7M5q7M7y7M967M/C7RBK7RDS7RFa7RHi7RJq7RL
/8u0Teu0Twu1USu1U0u1VWu1V4u1Wau1W8u1Xeu1Xwu2YSu2Y0u2ZWu2Z4u2aau2a8u2beu2bwu3cSu3c0u3dWu3d4u3eau3e8u3feu3fwu4gSu4g0u4
hWu4h4u4iau4i8u4jeu4jwu5kSu5k0u5lWu5l4u5mau5m8u5neu5nwu6oSu6o0u6pWu6p4u6qau6q8u6reu6rwu7sSu7s0u7tWu7t4u7uau7u8u7veu7
vwu8wSu8w0u8xWu8x4u8yau8y8u8zeu8zwu90Su900u91Wu914u92au928u93eu93wu+4Su+40u+5Wu+54u+6au+68u+7eu+7wu/8Su/80u/9f9rv/eL
v/mrv/vLv/3rv/8LwAEswANMwAVswAeMwAmswAvMwA3swA8MwREswRNMwRVswReMwRmswRvMwR3swR8MwiEswiNMwiVswieMwimswivMwi3swi8MwzEs
wzNMwzVswzeMwzmswzvMwz3swz8MxEEsxENMxEVsxEeMxEmsxEvMxE3sxE8MxVEsxVNMxVVsxVeMxVmsxVvMxV3sxV8MxmEsxmNMxmVsxmeMxmmsxmvM
xm3sxm8Mx3Esx3NMx3Vsx3eMx3msx3vMx33sx38MyIEsyINMyIVsyIeMyImsyIvMyI3syI8MyZEsyZNMyZVsyZeMyZmsyZvMyZ3syZ9nDMqhLMqjTMql
bMqnjMqprMqrzMqt7MqvDMuxLMuzTMu1bMu3jMu5rMu7zMu97Mu/DMzBLMzDTMzFbMzHjMzJrMzLzMzN7MzPDM3RLM3TTM3VbM3XjM3ZrM3bzM3d7M3f
DM7hDMAFAAAh+QQFCAABACwAAAAAAQABAAACAkwBACH5BAUIAAEALAAAAAABAAEAAAICTAEAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQAh+QQFCAABACwA
AAAAAQABAAACAkwBACH5BAkIAAEALBMBvQDrAKcBAAL/jI+py+0Po5y02ouz3rz7D4biSJbmiabqyrbuC8fyTNf2jef6zvf+DwwKh8Si8YhMKpfMpvMJ
jUqn1Kr1is1qt9yu9wsOi8fksvmMTqvX7Lb7DY/L5/S6/Y7P6/f8vv8PGCg4SFhoeIiYqLjI2Oj4CBkpOUlZaXmJmam5ydnp+QkaKjpKWmp6ipqqusra
6voKGys7S1tre4ubq7vL2+v7CxwsPExcbHyMnKy8zNzs/AwdLT1NXW19jZ2tvc3d7f0NHi4+Tl5ufo6err7O3u7+Dh8vP09fb3+Pn6+/z9/v/w8woMCB
BAsaPIgwocKFDBs6fAgxosSJFCtavIgxo8aNvRw7evwIMqTIkSRLmjyJMqXKlSxbunwJM6bMmTRr2ryJM6fOnTx7+vwJNKjQoUSLGj2KNKnSpUybOn0K
NarUqVSrWr2KNavWrVy7ev0KNqzYsWTLmj2LNq3atWzbun0LN67cuXTr2r2LN6/evXz7+v0LOLDgwYQLGz6MOLHixYwbO34MObLkyZQrW76MObPmzZw7
e/4MOrTo0aRLmz6NOrXq1axbu34NO7bs2bRr276NO7fu3bx7+/4NPLirAgAh+QQFCAABACwAAAAAOAQ4BAAC/4yPqcvtD6OctNqLs968+w+G4kiW5omm
6sq27gvH8kzX9o3n+s73/g8MCofEovGITCqXzKbzCY1Kp9Sq9YrNarfcrvcLDovH5LL5jE6r1+y2+w2Py+f0uv2Oz+v3/L7/DxgoOEhYaHiImKi4yNjo
+AgZKTlJWWl5iZmpucnZ6fkJGio6SlpqeoqaqrrK2ur6ChsrO0tba3uLm6u7y9vr+wscLDxMXGx8jJysvMzc7PwMHS09TV1tfY2drb3N3e39DR4uPk5e
bn6Onq6+zt7u/g4fLz9PX29/j5+vv8/f7/8PMKDAgQQLGjyIMKHChQwbOnwIMaLEiRQrWryIMaPGjf8cO3r8CDKkyJEkS5o8iTKlypUsW7p8CTOmzJk0
a9q8iTOnzp08e/r8CTSo0KFEixo9ijSp0qVMmzp9CjWq1KlUq1q9ijWr1q1cu3r9Cjas2LFky5o9izat2rVs27p9Czeu3Ll069q9izev3r18+/r9Cziw
4MGECxs+jDix4sWMGzt+DDmy5MmUK1u+jDmz5s2cO3v+DDq06NGkS5s+jTq16tWsW7t+DTu27Nm0a9u+jTu37t28e/v+DTy48OHEixs/jjy58uXMmzt/
Dj269OnUq1u/jj279u3cu3v/Dj68+PHky5s/jz69+vXs27t/Dz++/Pn069u/jz+//v38+/v//w9ggAIOSGCBBh6IYIIKLshggw4+CGGEEk5IYYUWXohh
hhpuyGGHHn4IYogijkhiiSaeiGKKKq7IYosuvghjjDLOSGONNt6IY4467shjjz7+CGSQQg5JZJFGHolkkkouyWSTTj4JZZRSTklllVZeiWWWWm7JZZde
fglmmGKOSWaZZp6JZppqrslmm26+CWeccs5JZ5123olnnnruyWeffv4JaKCCughAoYYWGsChii46w6KOAnCBo1U8iigClA5azqMGUMooDJwaisGlVHBq
qaSYkqPpAZ8e2sKqlYZq6qipbhrrqeHMqqqrkKagqwa4TkFqor/a6s2wwrqKArIb/4g6aarGEsvNs8euSkKvHdTarKTMQlustLruCoKy12Ir67fgcttt
pw5Y64G446p7hbnnotuNtLm6mwG+HGybrb70amNvAuxGSq0IAUPx7b/gHHxvwRX4+66iXAysMMAMl+qwBBBHzOoWFFeczcUCbzzypyaI7ATJIE+DMsYm
P5BxteTG+/LK27Tscs0KxCwzvFrobDM2OJcMdMMzjzA0E8EGHfLRFHw8rc8nO23F0kxfk/TO+GYNq9Q/U301NFwTbXXU8/IKdr8dh4312GQPC6oLbh/B
L9vUpG0B1C/MbUTddkfj97Iqo423FIH//QzfWvPcauFRKI44L5C/PfnDXv9nUXnkuGSeM+cQHK722ppL47nRoJdQOg+nj55M6rQyfoLrOqzO+jGyv152
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
i4ufjStr7gm5kTuek7uY82m5dkmdmYsFmMu5nVu5n2uYxSm6s7ibpbuQp4u6qcuaq7uIp+m66IiZsXuWjkm7Y2a7t0uQeqm7l/eWvRsvdbiVwIu42ji8
xFtN9caUyDuS7yaUzJu3LsWCJwm9ditcMVm9wllDmZm9ZAtFxtm92mthqAhwuhq+2tpHmLgudnW+alt5IzitT5O17Su+v/8Gi/TLl805vdCIv/kbp+Ta
v8n7vqEawK87ePlTwGI5wMmSwKy7wA0cd+4KwayGjRNcSPxrwRR8wBnMdBLMwdKFwR/MCOEkwlyFiCU8wiSMwokAhCsMwtnqwheswjEswzBMw+QXwjes
wzvMwz3swz8MxEEsxENMxEVsxEeMxEmsxEvMxE3sxE8MxVEsxVNMxVVsxVeMxVmsxVvMxV3sxV8MxmEsxmNMxmVsxmeMxmmsxmvMxm3sxm8Mx3Esx3NM
x3Vsx3eMx3msx3vMx33sx38MyIEsyINMyIVsyIeMyImsyIvMyI3syI8MyZEsyZNMyZVsyZeMyZmsyZvMyZ3syZ//DMqhLMqjTMqlbMqnjMqprMqrzMqt
7MqvDMuxLMuzTMu1bMu3jMu5rMu7zMu97Mu/DMzBLMzDTMzFbMzHjMzJrMzLzMzN7MzPDM3RLM3TTM3VbM3XjM3ZrM3bzM3d7M3fDM7hLM7jTM7lbM7n
jM7prM7rzM7t7M7vDM/xLM/zTM/1bM/3jM/5rM/7zM/97M//DNABLdADTdAFbdAHjdAJrdALzdAN7dAPDdERLdETTdEVbdEXjdEZrdEbzdEd7dEfDdIh
LdIjTdIlbdInjdIprdIrzdIt7dIvDdMxLdMzTdM1bdM3jdM5rdM7zdM97dM/DdRBLdRDTdRFbdRHjdRJ/a3US83UTe3UTw3VUS3VU03VVW3VV43VWa3V
W83VXe3VXw3WYS3WY03WZW3WZ43Waa3Wa83Wbe3Wbw3XcS3Xc03XdW3Xd43Xea3Xe83Xfe3Xfw3YgS3Yg03YhW3Yh43Yia3Yi83Yje3Yjw3ZkS3Zk03Z
lW3Zl43Zma3Zm83Zne3Znw3aoS3ao03apW3ap43aqa3aq83are3arw3bsS3bs03btW3bt43bua3bu83bve3bvw3cwS3cw03cxW3cx43cya3cy83cze3c
zw3d0S3d003d1W3d143d2a3d283d3e3d3w3e4S3e403e5W3e543e6a3e683e7e3e781eBQAAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQAh+QQFCAABACwA
AAAAAQABAAACAkwBACH5BAUIAAEALAAAAAABAAEAAAICTAEAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQAh+QQJCAABACzLAFYBoAENAQAC/4yPqcvtD6Oc
tNqLs968+w+G4kiW5omm6sq27gvH8kzX9o3n+s73/g8MCofEovGITCqXzKbzCY1Kp9Sq9YrNarfcrvcLDovH5LL5jE6r1+y2+w2Py+f0uv2Oz+v3/L7/
DxgoOEhYaHiImKi4yNjo+AgZKTlJWWl5iZmpucnZ6fkJGio6SlpqeoqaqrrK2ur6ChsrO0tba3uLm6u7y9vr+wscLDxMXGx8jJysvMzc7PwMHS09TV1t
fY2drb3N3e39DR4uPk5ebn6Onq6+zt7u/g4fLz9PX29/j5+vv8/f7/8PMKDAgQQLGjyIMKHChQwbOnwIMaLEiRQrWryIMaPGjdwcO3r8CDKkyJEkS5o8
iTKlypUsW7p8CTOmzJk0a9q8iTOnzp08e/r8CTSo0KFEixo9ijSp0qVMmzp9CjWq1KlUq1q9ijWr1q1cu3r9Cjas2LFky5o9izat2rVs27p9Czeu3Ll0
69q9izev3r18+/r9Cziw4MGECxs+jDix4sWMGzt+DDmy5MmUK1u+jDmz5s2cO3v+DDq06NGkS5s+jTq16tWsW7t+DTu27Nm0a9u+jTu37t28e/v+DTy4
8OHEixs/jjy58uXMmzt/Dj269OnUq1u/jj279u3cMRYAACH5BAUIAAEALAAAAAA4BDgEAAL/jI+py+0Po5y02ouz3rz7D4biSJbmiabqyrbuC8fyTNf2
jef6zvf+DwwKh8Si8YhMKpfMpvMJjUqn1Kr1is1qt9yu9wsOi8fksvmMTqvX7Lb7DY/L5/S6/Y7P6/f8vv8PGCg4SFhoeIiYqLjI2Oj4CBkpOUlZaXmJ
mam5ydnp+QkaKjpKWmp6ipqqusra6voKGys7S1tre4ubq7vL2+v7CxwsPExcbHyMnKy8zNzs/AwdLT1NXW19jZ2tvc3d7f0NHi4+Tl5ufo6err7O3u7+
Dh8vP09fb3+Pn6+/z9/v/w8woMCBBAsaPIgwocKFDBs6fAgxosSJFCtavIgxo8aN/xw7evwIMqTIkSRLmjyJMqXKlSxbunwJM6bMmTRr2ryJM6fOnTx7
+vwJNKjQoUSLGj2KNKnSpUybOn0KNarUqVSrWr2KNavWrVy7ev0KNqzYsWTLmj2LNq3atWzbun0LN67cuXTr2r2LN6/evXz7+vUFAMDfwUMDEz7sM7Bh
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
J7sKjwet5BbEO8zDTevDwmfER4zE55uyMLyKEOfENOy/LWl2U/xxVWzFLhwK82e2LsfFJnzCGeJ1YWx0Y4zEuiBtQiBwatzFXnwKbayOaQxyWwvFigdd
a7PFTZfH9pO+PQzGCWnHNRDIqhu+5YZv5nvDgTvIjuy1zZTIemwhYhwDdPxz65uHcrwJrsjI3v+nL48sppq8yWV8Ccd2cV6XLVn8dawsuiAsCTgqIyZA
xCPgc3j8Cgf8xh5agNp5cio5yT+cmwn3mhWMeZ98eX98QIdMrzinbxJMyr6IzP/XVszczA8KxF27uFSCvcHsejZKbswhwo01zarMv9Z8zZnKJtiVgqmc
cMpMROiMoPJbzFcJz1Ekz+l8xf+6m2Ocz/q8z+VKM/c8R/8M0LBMxu9C0HbrzTF8wLbMLQs9SQY9z/T8kh0Mx0+MCwAcKeW8bBmNtzXcwLCLeB5tuSDd
yiI90pHclcaLXSh90QjNCdEM0xIa0JjA0TVtupxMITT9whJNQCz908PrCDItURSteT7/bWRJbE1CPQnGzK9M3dQNTb5EfZw3vXVUXdVVK8hOzXmm3AqN
PLhaHc9IbcNWzb1e/dVSTQm93LtcrdOoANRxTdd1bdd3jdd5rdd7zdd97dd/DdiBLdiDTdiFbdiHjdiJrdiLzdiN7diPDdmRLdmTTdmVbdmXjdmZrdmb
zdmd7dmfDdqhLdqjTdqlbdqnjdqprdqrzdqt7dqvDduxLduzTdu1bdu3jdu5rdu7zdu97du/DdzBLdzDTdzFbdzHjdzJrdzLzdzN7dzPDd3RLd3TTd3V
bd3Xjd3Zrd3bzd3d7d3fDd7hLd7jTd7lbd7njd7prd7rzd7t7d7vDd/xLd/z/03f9W3f943f+a3f+83f/e3f/w3gAS7gA07gBW7gB47gCa7gC87gDe7g
Dw7hES7hE07hFW7hF47hGa7hG87hHe7hHw7iIS7iI07iJW7iJ47iKa7iK87iLe7iLw7jMS7jM07jNW7jN47jOa7jO87jPe7jPw7kQS7kQ07kRW7kR47k
Sa7kS87kTe7kTw7lUS7lU07lVW7lV47lWa7lW87lXe7lXw7mYS7mY07mZW7mZ47maa7ma87mbe7mbw7ncS7nc07ndW7nd47nea7ne87nfe7nfw7ogS7o
g07ohW7oh47oia7oi87oje7ojw7pkS7pk07plW7pl47pma7pm87pnf/u6Z8O6qEu6qNO6qVu6qeO6qmu6qvO6q3u6q8O67Eu67NO67Vu67eO67mu67vO
673u678O7MEu7MNO7MVu7MeO7Mmu7MvO7M3u7M8O7dEu7dNO7dVu7deO7dmu7dvO7d3u7d8O7uEu7uNO7uVu7ueO7umu7uvO7u3u7u8O7/Eu7/NO7/Vu
7/eO7/mu7/vO7/3u7/8O8AEv8ANP8AVv8AeP8Amv8AvP8A3v8A8P8REv8RNP8RVv8ReP8Rmv8RvP8R3v8R8P8iEv8iNP8iVv8ieP8imv8ivP8i3v8i8P
8zEv8zNP8zVv8zeP8zmv8zvP8z3v8z8P9EEv9ENP9EVv9Ec7j/RJr/RLz/RN7/RPD/VRL/VTT/VVb/VXj/VZr/Vbz/Vd7/VfD/ZhL/ZjT/Zlb/Znj/Zp
r/Zrz/ZJVQAAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQAh+QQFCAABACwAAAAAAQABAAACAkwBACH5BAUIAAEALAAAAAABAAEAAAICTAEAIfkEBQgAAQAs
AAAAAAEAAQAAAgJMAQAh+QQJCAABACzYAUIAhAEJAgAC/4yPqcvtD6OctNqLs968+w+G4kiW5omm6sq27gvH8kzX9o3n+s73/g8MCofEovGITCqXzKbz
CY1Kp9Sq9YrNarfcrvcLDovH5LL5jE6r1+y2+w2Py+f0uv2Oz+v3/L7/DxgoOEhYaHiImKi4yNjo+AgZKTlJWWl5iZmpucnZ6fkJGio6SlpqeoqaqrrK
2ur6ChsrO0tba3uLm6u7y9vr+wscLDxMXGx8jJysvMzc7PwMHS09TV1tfY2drb3N3e39DR4uPk5ebn6Onq6+zt7u/g4fLz9PX29/j5+vv8/f7/8PMKDA
gQQLGjyIMKHChQwbOnwIMaLEiRQrWryIMaPGjf8cO3r8CDKkyJEkS5o8iTKlypUsW7p8CTOmzJk0a9q8iTOnzp08e/r8CTSo0KFEixo9ijSp0qVMmzp9
CjWq1KlUq1q9ijWr1q1cu3r9Cjas2LFky5o9izat2rVs27p9Czeu3Ll069q9izev3r18+/r9Cziw4MGECxs+jDix4sWMGzt+DDmy5MmUK1u+jDmz5s2c
O3v+DDq06NGkS5s+jTq16tWsW7t+DTu27Nm0a9u+jTu37t28e/v+DTy48OHEixs/jjy58uXMmzt/Dj269OnUq1u/jj279u3cu3v/Dj68+PHky5s/jz69
+vXs27t/Dz++/Pn069u/jz+//v38+/uk/w9ggAIOSGCBBh6IYIIKLshggw4+CGGEEk5IYYUWXohhhhpuyGGHHn4IYogijkhiiSaeiGKKKq7IYosuvghj
jDLOSGONNt6IY4467shjjz7+CGSQQg5JZJFGHolkkkouyWSTTj4JZZRSTklllVZeiWWWWm7JZZdefglmmGKOSWaZZp6JZppqrslmm26+CWeccs5JZ512
3olnnnruyWeffv7pTQEAIfkEBQgAAQAsAAAAADgEOAQAAv+Mj6nL7Q+jnLTai7PevPsPhuJIluaJpurKtu4Lx/JM1/aN5/rO9/4PDAqHxKLxiEwql8ym
8wmNSqfUqvWKzWq33K73Cw6Lx+Sy+YxOq9fstvsNj8vn9Lr9js/r9/y+/w8YKDhIWGh4iJiouMjY6PgIGSk5SVlpeYmZqbnJ2en5CRoqOkpaanqKmqq6
ytrq+gobKztLW2t7i5uru8vb6/sLHCw8TFxsfIycrLzM3Oz8DB0tPU1dbX2Nna29zd3t/Q0eLj5OXm5+jp6uvs7e7v4OHy8/T19vf4+fr7/P3+//DzCg
wIEECxo8iDChwoUMGzp8CDGixIkUK1q8iDGjxo3/HDt6/AgypMiRJEuaPIkypcqVLFu6fAkzpsyZNGvavIkzp86dPHv6/Ak0qNChRIsaPYo0qdKlTJs6
fQo1qtSpVKtavYo1q9atXLt6/Qo2rNixZMuaPYs2rdq1bNu6fQs3rty5dOvavYs3r969fPv6rQUgsGDBBgYb/os4puHBhRcHTgyZpePHByYDiIz5pGUE
mzN7DtmZ8+TPpDuGrjy6tOqLp0U7Xg17YmvUr2Pbbjjb9eLbvBPmpr27t/CBv3UfHo7cX3Hgx5M7x7fcOOPn1OlFZz69unZ316UT3g5eXXfs38ObLzee
POXz7L+lV3+5vXxuqS3Un4+/2nvv6/P7/4e2H3z/DehMgPzFR2CCxxgooIIODsPggXqU92CFQ0TY4B3BWchhDxhKWMd9HY54w4cZzmEiiSp6IOIGKW5h
GYIrzujCi43VBkeM/dHIYwotcvDjGjrK2GORJNh4IhtD7mhkkywGqQGSUyzJpJNWYiBlkmhQWeWVXk6Q5Y0bpsFll1+e2UCYWpJRJoVovsmAmmI2Z0ab
bsKJp3ooQPmFjnPemSeccq7ZZ4x6BorooH8C6oWfhCJ6paKPwmgof5DiKemiZmbhaAJ8XmrkpyGIakWnloJ6ZqaTllqpAqSiuuKrIMgKhame0gqrhaqC
SGl0uOb64K6rRmHrAr8Cq+CxHf8IK0OxxuKILI/MDtuEs8+OGe2M0x56RasQbJstbOBqSqQU3kagbLjyjRsAu3ue+y206naY7gf1/mCtA/fOC5677e67
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
lV6a6aadfhrqqKWemuqqrWYAaWCu3prrrr3+GuywxR6b7LLNPrsztNVem+223X4b7rjlnpvuuu2+G++89d6b7779/hvwwAUfnPDCDT8c//HEFV+c8cYd
fxzyyCWfnPLKLb8c88w135zzzj3/HPTQRR+d9NJNPx311FVfnfXWXX8d9thln5322m2/Hffcdd+d9959/x344IUfnvjijT8e+eSVX5755p1/HvropZ+e
+uqtvx777LXfnvvuvf8e/PDFH5/88s0/H/301V+f/fbdfx/++OWfn/767b8f//z135///v3/H4ABFOAACVhAAx4QgQlU4AIZ2EAHPhCCEZTgBClYQQte
EIMZ1OAGOdhBD34QhCEU4QhJWEITnhCFKVThClnYQhe+EIYxlOEMaVhDG94QhznU4Q552EMf/hCIQRTiEIlYRCMeEYlJVOISmf/YRCc+EYpRlOIUqVhF
K14Ri1nU4ha52EUvfhGMYRTjGMlYRjOeEY1pVOMa2dhGN74RjnGU4xzpWEc73hGPedTjHvnYRz/+EZCBFOQgCVlIQx4SkYlU5CIZ2UhHPhKSkZTkJClZ
SUteEpOZ1OQmOdlJT34SlKEU5ShJWUpTnhKVqVTlKlnZSle+EpaxlOUsaVlLW94Sl7nU5S552Utf/hKYwRTmMIlZTGMeE5nJVOYymdlMZz4TmtGU5jSp
WU1rXhOb2dTmNrnZTW9+E5zhFOc4yVlOc54TnelU5zrZ2U53vhOe8ZTnPOlZT3veE5/51Oc++dlPf/4ToAEV6EAJWlD/gx4UoQlV6EIZ2lCHPhSiEZXo
RClaUYteFKMZ1ehGOdpRj34UpCEV6UhJWlKTnhSlKVXpSlnaUpe+FKYxlelMaVpTm94UpznV6U552lOf/hSoQRXqUIlaVKMeFalJVepSmdpUpz4VqlGV
6lSpWlWrXhWrWdXqVrnaVa9+FaxhFetYyVpWs54VrWlV61rZ2la3vhWucZXrXOlaV7veFa951ete+dpXv/4VsIEV7GAJW1jDHhaxiVXsYhnbWMc+FrKR
lexkKVtZy14Ws5nV7GY521nPfha0oRXtaElbWtOeFrWpVe1qWdta174WtrGV7WxpW1vb3ha3udXtbnnbW9/+2ha4wRXucIlbXOMeF7nJVe5ymdtc5z4X
utGV7nSpW13rXhe72dXudrnbXe9+F7zhFe94yVte854XvelV73rZ2173vhe+8ZXvfOlbX/veF7/51e9++dtf//4XwAEW8IAJXGADHxjBCVbwghncYAc/
GMIRlvCEKVxhC18YwxnW8IY53GEPfxjEIRbxiElcYhOfGMUpVvGKWdxiF78YxjGW8YxpXGMb3xjHOdbxjnncYx//GMhBFvKQiVxkIx8ZyUlW8pKZ3GQn
PxnKUZbylKlcZStfGctZ1vKWI1gAACH5BAUIAAEALAAAAAABAAEAAAICTAEAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQAh+QQFCAABACwAAAAAAQABAAAC
AkwBACH5BAUIAAEALAAAAAABAAEAAAICTAEAIfkECQgAAQAsJAIFAUoBUgEAAv+Mj6nL7Q+jnLTai7PevPsPhuJIluaJpurKtu4Lx/JM1/aN5/rO9/4P
DAqHxKLxiEwql8ym8wmNSqfUqvWKzWq33K73Cw6Lx+Sy+YxOq9fstvsNj8vn9Lr9js/r9/y+/w8YKDhIWGh4iJiouMjY6PgIGSk5SVlpeYmZqbnJ2en5
CRoqOkpaanqKmqq6ytrq+gobKztLW2t7i5uru8vb6/sLHCw8TFxsfIycrLzM3Oz8DB0tPU1dbX2Nna29zd3t/Q0eLj5OXm5+jp6uvs7e7v4OHy8/T19v
f4+fr7/P3+//DzCgwIEECxo8iDChwoUMGzp8CDGixIkUK1q8iDGjxo3bHDt6/AgypMiRJEuaPIkypcqVLFu6fAkzpsyZNGvavIkzp86dPHv6/Ak0qNCh
RIsaPYo0qdKlTJs6fQo1qtSpVKtavYo1q9atXLt6/Qo2rNixZMuaPYs2rdq1bNu6fQs3rty5dOvavYs3r969fPv6/Qs4sODBhAsbPow4seLFjBs7fgw5
suTJlCtbvow5s+bNnDt7/gw6tOjRpEubPo06terVrFu7fg07tuzZtGvbvo07t+7dvHv7/g08uPDhxIsbP448ufLlzJs7fw49uvTp1Ktbv449u/bthAsA
ACH5BAUIAAEALAAAAAA4BDgEAAL/jI+py+0Po5y02ouz3rz7D4biSJbmiabqyrbuC8fyTNf2jef6zvf+DwwKh8Si8YhMKpfMpvMJjUqn1Kr1is1qt9yu
9wsOi8fksvmMTqvX7Lb7DY/L5/S6/Y7P6/f8vv8PGCg4SFhoeIiYqLjI2Oj4CBkpOUlZaXmJmam5ydnp+QkaKjpKWmp6ipqqusra6voKGys7S1tre4ub
q7vL2+v7CxwsPExcbHyMnKy8zNzs/AwdLT1NXW19jZ2tvc3d7f0NHi4+Tl5ufo6err7O3u7+Dh8vP09fb3+Pn6+/z9/v/w8woMCBBAsaPIgwocKFDBs6
fAgxosSJFCtavIgxo8aN/xw7evwIMqTIkSRLmjyJMqXKlSxbunwJM6bMmTRr2ryJM6fOnTx7+vwJNKjQoUSLGj2KNKnSpUybOn0KNarUqVSrWr2KNavW
rVy7ev0KNqzYsWTLmj2LNq3atWzbun0LN67cuXTr2r2LN6/evXz7+v0LOLDgwYQLGz6MOLHixYwbO34MObLkyZQrW76MObPmzZw7e/4MOrTo0aRLmz6N
OrXq1axbu34NO7bs2bRr276NO7fu3bx7+/4NPLjw4cSLGz+OPLny5cybO38OPbr06dSrW7+OPbv27dy7e/8OPrz48eTLmz+PPr369ezbu38PP778+fTr
27+PP7/+/fz7+///D2CAAg5IYIEGHohgggouyGCDDj4IYYQSTkhhhRZeiGGGGm7IYYcefghiiCKOSGKJJp6IYooqrshiiy6+CGOMMs5IY4023ohjjjru
yGOPPv4IZJBCDklkkUYeiWSSSi7JZJNOPglllFJOSWWVVl6JZZZabslll15+CWaYYo5JZplmnolmmmquyWabbr4JZ5xyzklnnXbeiWeeeu7JZ59+/glo
oIIOShsAhOoGgKGH2pZooovW1qijj8YWaaOTvlZppJe2lqmmm6bWaaWfohaqp6OSVmqmp46WqqirgtZqp696Fquqs25Wq6y3ZparrrtW1muovwIbrK/D
PlZsqcf/Qpassssy1myqzy4WrbPTIlattNcWlq222w7Wrbff/hVuq+MCVq655/aVrrrr5tVurO/CG6+7885Vr7z30pWvvvvG1a+//7YVcK0Dv1WwwAej
lbDBCzPcsMMPlxVxrhNTXLHCF3+VscQbe9WxxR+DHLLHI2dVssgno5yyyStP1XKvL1sVs8ozS1WzzTc/lbPOOy/Vs8w/NxW00EMrVbTRRx+VtM9LD9W0
00//FLXSUwNVtdVX95S11lvr1LXXX98UttRj11S22WfHlLbYa8PUtttvtxS33HOrVLfad6eUt957m9S333+PFLjgg39UuN2HI564y4uT1Ljhj2MUudSK
/06uUeWSY06R5ptzHpHnjoN+i7BRiF4wA7aSXorpT6Ce+gKrsz6K603AHnsCttMOyu5K4J47Atby/snwSQAf/AHiEs+J8UcgH3ADzjOfyfREQB+96tZT
X8n2QGCfvO7ecy+J70OAn7325pNPyfrfo9/vA8uz376x58OfbwTz0x+J+zzgnz75+Y9/jBigDgAYP/2Nj4CJWOANEJhABRqQgYZwYA0gmD8KWJCCg9ig
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
WZZLd5QoGDRcCXwjyTlt6ZY5WXdfSVZyuYotGV5ZuYxnKZZjmYcImWU1M5d0WVxpCZYLl5d6+T6G2Y0x85csCJhNqJiHiFZlh5hCWTKLyZCLxZdkVJmZ
2JmeuZYyuJmWyZgtV5onZXCiOZo/9JkD2TGo+Zo4AJeTE3CCWJu2GZseGHGuuY5Z15tMNJt3eJlcJpju2DC6GZyN6ZjM9JtbeJx3l5lI+XM6OJ0c95zQ
uZzSmZpntp35151gmJ3aGZ7xlzDeuZtfV51smXNKuJ72ZZfuGUM0qDh4/7aaNolv8BmfmNmepvmeKhhViTecLRigFXSfmzefLxVAAlqeiLagDFqfIJig
A1WgSIh1JVihkXahJDShEfigFpqfCpWhFBqiIjqi+1SiIHqiKBqhWUh5JtqiHHqegGh0LWR631ejwbefOLqhhXSbf3OjA5ijXdShFDSkfWB9ZXSkDNSj
SrqkRtqkhQh3xRalTDqlr1ilywZ99/ei+lmk4BamYvqlJKp5+zemZFqmi8h62nalamqNZtmlbpqmXJql7HOm5/emdrqjhBSjelqnVtqnfSl4Urqngpqi
7NSm1zenFhikc7OoxdeoMvqfMDqgNHqoB/iobxOpQCp72Hmn4v/4qWjQqQg6qHQ0qr83qaB6qqC5qraWqRraqrIZqJgaqy84q755q2v2quSZqNzUqxAa
rL66poq6q4NXqJxZrMb6o+aZrMq6rMB6rEuQpywZqtQzrWv3p8WzqWtTq9Q5rJEZp4n5nSdXqtaaq7parvJ5ruiarsQ5o8JVrafQrZy6rnu5pakwmVra
nJmVr6uwr3jar1f3pLBQr/YasFL3CgErsHKnsLFwsAj7rhP3sLLAsA07sV6JeLpwseSDZh/KsRErsZV6eCB7TLXQsR77q744r5tosSI7ssn5dkEks2RZ
s6KQsipLsrGXPzdrs+OKs9dKpUDbeAVbjdHqCDmrsz7/y7JJKo9Cy6gZ66pMS7HLSbVzCLNXBrX8Q7S6E4UHSpP92QhKq7LEGDGHubVqQLYeKz1fm55X
+4xru3xZK6Rua7VAJ7d9RLeQCplw+7R5q7dpy7W5KZV7+3CGG7M55LdfNrDQurPWmUqLO7OAS6qIm7gdJbl1SbmVK7hD+61Nt7mcK7VaKSyZm1+XWjuW
K7EYhwTNSq+q+22xK7uzS7u1a7u3i7u5q7u7y7u967u/C7zBK7zDS7zFa7zHi7zJq7zLy7zN67zPC73RK73TS73Va73Xi73Zq73by73d673fC77hK77j
S77la77ni77pq77ry77t677vC7/xK7/zS7/1/2u/94u/+au/+8u//eu//wvAASzAA0zABWzAB4zACazAC8zADezADwzBESzBE0zBFWzBF4zBGazBG8zB
HezBHwzCISzCI0zCJWzCJ4zCKazCK8zCLezCLwzDMSzDM0zDNWzDN4zDOazDO8zDPezDPwzEQSzEQ0zERWzER4zESazES8zETezETwzFUSzFU0zFVWzF
V4zFWazFW8zFXezFXwzGYSzGY0zGZWzGZ4zGaazGa8zGbezGbwzHcSzHc0zHdWzHd4zHeazHe8zHfezHfwzIgSzIg0zIhWzIh4zIiazIi8zIjezIjwzJ
kSzJk0zJlWzJl4zJmazJm8zJnezJn/8MyqEsyqNMyqVsyqeMyqmsyqvMyq3syq8My7Esy7NMy7Vsy7eMy7msy7vMy73sy78MzMEszMNMzMVszMeMzMms
zMvMzM3szM8MzdEszdNMzdVszdeMzdmszdvMzd3szd8MzuEszuNMzuVszueMzumszuvMzu3szu8Mz/Esz/NMz/Vsz/eMz/msz/vMz/3sz/8M0AEt0ANN
0AVt0AeN0Amt0AvN0A3t0A8N0REt0RNN0RVt0ReN0Rmt0RvN0R3t0R8N0iEt0iNN0iVt0ieN0imt0ivN0i3t0i8N0zEt0zNN0zVt0zeN0zmt0zvN0z3t
0z8N1EEt1ENN1EVt1EeN1El5rdRLzdRN7dRPDdVRLdVTTdVVbdVXjdVZrdVbzdVd7dVfDdZhLdZjTdZlbdZnjdZprdZrzdZt7dZvDddxLddzTdd1bdd3
jdd5rdd7zdd97dd/DdiBLdiDTdiFbdiHjdiJrdiLzdiN7diPDdmRLdmTTdmVbdmXjc4FAAAh+QQFCAABACwAAAAAAQABAAACAkwBACH5BAUIAAEALAAA
AAABAAEAAAICTAEAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQAh+QQFCAABACwAAAAAAQABAAACAkwBACH5BAkIAAEALDICvQDnAHABAAL/jI+py+0Po5y0
2ouz3rz7D4biSJbmiabqyrbuC8fyTNf2jef6zvf+DwwKh8Si8YhMKpfMpvMJjUqn1Kr1is1qt9yu9wsOi8fksvmMTqvX7Lb7DY/L5/S6/Y7P6/f8vv8P
GCg4SFhoeIiYqLjI2Oj4CBkpOUlZaXmJmam5ydnp+QkaKjpKWmp6ipqqusra6voKGys7S1tre4ubq7vL2+v7CxwsPExcbHyMnKy8zNzs/AwdLT1NXW19
jZ2tvc3d7f0NHi4+Tl5ufo6err7O3u7+Dh8vP09fb3+Pn6+/z9/v/w8woMCBBAsaPIgwocKFDBs6fAgxosSJFCtavIgxo8aNlxw7evwIMqTIkSRLmjyJ
MqXKlSxbunwJM6bMmTRr2ryJM6fOnTx7+vwJNKjQoUSLGj2KNKnSpUybOn0KNarUqVSrWr2KNavWrVy7ev0KNqzYsWTLmj2LNq3atWzbun0LN67cuXTr
2r2LN6/evXz7+v0LOLDgwYQLGz6MOLHixYwbO34MObLkyZQrW76MObPmzZw7e/6stQAAIfkEBQgAAQAsAAAAADgEOAQAAv+Mj6nL7Q+jnLTai7PevPsP
huJIluaJpurKtu4Lx/JM1/aN5/rO9/4PDAqHxKLxiEwql8ym8wmNSqfUqvWKzWq33K73Cw6Lx+Sy+YxOq9fstvsNj8vn9Lr9js/r9/y+/w8YKDhIWGh4
iJiouMjY6PgIGSk5SVlpeYmZqbnJ2en5CRoqOkpaanqKmqq6ytrq+gobKztLW2t7i5uru8vb6/sLHCw8TFxsfIycrLzM3Oz8DB0tPU1dbX2Nna29zd3t
/Q0eLj5OXm5+jp6uvs7e7v4OHy8/T19vf4+fr7/P3+//DzCgwIEECxo8iDChwoUMGzp8CDGixIkUK1q8iDGjxo3/HDt6/AgypMiRJEuaPIkypcqVLFu6
fAkzpsyZNGvavIkzp86dPHv6/Ak0qNChRIsaPYo0qdKlTJs6fQo1qtSpVKtavYo1q9atXLt6/Qo2rNixZMuaPYs2rdq1bNu6fQs3rty5dOvavYs3r969
fPv6/Qs4sODBhAsbPow4seLFjBs7fgw5suTJlCtbvow5s+bNnDt7/gw6tOjRpEubPo06terVrFu7fg07tuzZtGvbvo07t+7dvHv7/g08uPDhxIsbP448
ufLlzJs7fw49uvTp1Ktbv449u3ZNAABs/66ou3fw5AmJH18+/Z/z6NW7v8O++/v5duK3p4+/jf37+fuf/9nPn38ChgGgfAMeCEaBBiLIoBYKLthghFQ8
CKGEFjpBYYUXbohEhuJxCGKHHgYYYok8jKihiSrigGKKK74oQ4suwkgjCzJ+WGOOLdyIo44+nsBjjz8OKUKQMxKJ5AVGHplkkxEsyaSTUi4ApZBTXslA
lVZiyaUBWm7ZJZZfRhkmkWOSWaaPZ4KZpplrktjmj2+yGaeOc6JZp4l34pkniHvy2eeFfwIaaISD0lmon4fCmeiGixLaqH+PQhopfpNSWul7l2KaqXqb
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
/wlTnwqgmmqWlWiqy8mk51hnpKqKqXqqK+WqFAir/Cmroaqqq8qgGjWrssineglStXqAvdqVvIqrpCmshClRyQp9Prmd45klx1qqzIqYWEWsIPmrnnmK
0lqeGOqcFHWtJHmVzwoB4MqtvhqujGlc1Iqt2cqbNcWu6hivrYlM8zqH40quugkh9nqG3pqvdzifuhoE//ivmlObfuqIzlqwgnOwnFpSL7mwKtiwAutM
TBixIMSv1kiwFws4+UmxPWmxHKs6/omdkgixIltGJGuex3iyKBtFDqs4G+uyM0uzNWuzN4uzOauzO8uzPeuzPwu0QSu0Q0u0RWu0R4u0Sau0S//LtE3r
tE8LtVErtVNLtVVrtVeLtVmrtVvLtV3rtV8LtmErtmNLtmVrtmeLtmmrtmvLtm3rtm8Lt3Ert3NLt3Vrt3eLt3mrt3vLt33rt38LuIEruINLuIVruIeL
uImruIvLuI3ruI8LuZEruZNLuZVruZeLuZmruZvLuZ3ruZ8LuqEruqNLuqVruqeLuqmruqvLuq3ruq8Lu7Eru7NLu7Vru7eLu7mru7vLu73ru78LvMEr
vMNLvMVrvMeLvMmrvMvLvM3rvM8LvdErvdNLvdVrvdeLvdmrvdvLvd3rvd8LvuErvuNLvuVrvueLvumrvuvLvu3rvu8Lv/Erv/NLv/X/a7/3i7/5q7/7
y7/967//C8ABLMADTMAFbMAHjMAJrMALzMAN7MAPDMERLMETTMEVbMEXjMEZrMEbzMEd7MEfDMIhLMIjTMIlbMInjMIprMIrzMIt7MIvDMMxLMMzTMM1
bMM3jMM5rMM7zMM97MM/DMRBLMRDTMRFbMRHjMRJrMRLzMRN7MRPDMVRLMVTTMVVbMVXjMVZrMVbzMVd7MVfDMZhLMZjTMZlbMZnjMZprMZrzMZt7MZv
DMdxLMdzTMd1bMd3jMd5rMd7zMd97Md/DMiBLMiDTMiFbMiHjMiJrMiLzMiN7MiPDMmRLMmTTMmVbMmXjMmZrMmbzMmd7MmfwgzKoSzKo0zKpWzKp4zK
qazKq8zKrezKrwzLsSzLs0zLtWzLt4zLuazLu8zLvezLvwzMwSzMw0zMxWzMx4zMyazMy8zMzezMzwzN0SzN00zN1WzN14zN2azN28zN3ezN3wzO4SzO
40zO5WzO54zO6azO68zO7ezO7wzP8SzP80zP9WzP94zP+azP+8zP/ezP/wzQAS3QA03QBW3QB43QCa3QC83QDe3QDw3RES3RE03RFW3RF43RGa3RG83R
He3RblwAACH5BAUIAAEALAAAAAABAAEAAAICTAEAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQAh+QQFCAABACwAAAAAAQABAAACAkwBACH5BAUIAAEALAAA
AAABAAEAAAICTAEAIfkEBQgAAQAsAAAAAAEAAQAAAgJMAQA7
""".strip()


class CTkFileDialog(ctk.CTkToplevel):
    """Einfacher, zum App-Look passender Datei-Dialog (Speichern/Öffnen)
    als Ersatz für den nativen tkinter.filedialog, der das System-Dark-
    Theme nicht übernimmt. Kein vollwertiger Dateimanager - einfache
    Verzeichnisnavigation, Dateiliste, Endungsfilter, Dateiname-Feld."""

    def __init__(self, parent, mode="save", title="Datei wählen",
                 initial_dir=None, initial_file="", filetypes=(("Alle Dateien", "*"),)):
        super().__init__(parent)
        self.mode = mode  # "save" oder "open"
        self.filetypes = filetypes
        self.result_path = None
        self.current_dir = os.path.abspath(initial_dir or os.path.expanduser("~"))

        self.title(title)
        self.geometry("640x480")
        self.transient(parent)
        self.grab_set()

        top_row = ctk.CTkFrame(self, fg_color="transparent")
        top_row.pack(fill="x", padx=16, pady=(16, 8))
        ctk.CTkButton(top_row, text="⬆ Übergeordnet", width=130, corner_radius=10,
                      command=self._go_up).pack(side="left")
        self.path_var = tk.StringVar(value=self.current_dir)
        path_entry = ctk.CTkEntry(top_row, textvariable=self.path_var, corner_radius=10)
        path_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))
        path_entry.bind("<Return>", lambda e: self._navigate_to(self.path_var.get()))

        self.listbox_frame = ctk.CTkScrollableFrame(self, corner_radius=10)
        self.listbox_frame.pack(fill="both", expand=True, padx=16, pady=8)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(4, 16))
        ctk.CTkLabel(bottom, text="Dateiname:").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.filename_var = tk.StringVar(value=initial_file)
        fn_entry = ctk.CTkEntry(bottom, textvariable=self.filename_var, corner_radius=10)
        fn_entry.grid(row=0, column=1, sticky="we", padx=8, pady=(0, 8))
        fn_entry.bind("<Return>", lambda e: self._confirm())
        bottom.grid_columnconfigure(1, weight=1)

        btn_row = ctk.CTkFrame(bottom, fg_color="transparent")
        btn_row.grid(row=1, column=0, columnspan=2, sticky="e")
        ctk.CTkButton(btn_row, text="Abbrechen", corner_radius=10, fg_color="gray30",
                      command=self._cancel).pack(side="right", padx=(8, 0))
        action_label = "Speichern" if mode == "save" else "Öffnen"
        ctk.CTkButton(btn_row, text=action_label, corner_radius=10,
                      command=self._confirm).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._populate(self.current_dir)

    def _matches_filter(self, filename):
        for _, pattern in self.filetypes:
            exts = [e.strip("*") for e in pattern.split()]
            if any(filename.endswith(e) for e in exts) or "*" in pattern:
                return True
        return False

    def _populate(self, directory):
        for w in self.listbox_frame.winfo_children():
            w.destroy()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            ctk.CTkLabel(self.listbox_frame, text=f"Konnte Verzeichnis nicht lesen: {e}",
                         text_color="#EF5350").pack(anchor="w", padx=6, pady=4)
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                text, icon = entry.name, "📁"
                cmd = lambda p=entry.path: self._navigate_to(p)
            else:
                if self.mode == "open" and not self._matches_filter(entry.name):
                    continue
                text, icon = entry.name, "📄"
                cmd = lambda n=entry.name: self._select_file(n)
            ctk.CTkButton(self.listbox_frame, text=f"{icon}  {text}", anchor="w",
                          fg_color="transparent", hover_color="#2a2a3a",
                          corner_radius=6, command=cmd).pack(fill="x", padx=2, pady=1)
        self.current_dir = directory
        self.path_var.set(directory)

    def _navigate_to(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            self._populate(path)

    def _go_up(self):
        self._navigate_to(os.path.dirname(self.current_dir.rstrip("/")) or "/")

    def _select_file(self, filename):
        self.filename_var.set(filename)
        if self.mode == "open":
            self._confirm()

    def _confirm(self):
        name = self.filename_var.get().strip()
        if not name:
            return
        self.result_path = os.path.join(self.current_dir, name)
        if self.mode == "open" and not os.path.isfile(self.result_path):
            return
        self.destroy()

    def _cancel(self):
        self.result_path = None
        self.destroy()

    @staticmethod
    def ask_save_filename(parent, initial_dir=None, initial_file="",
                           filetypes=(("Alle Dateien", "*"),)):
        dlg = CTkFileDialog(parent, mode="save", title="Speichern unter",
                             initial_dir=initial_dir, initial_file=initial_file,
                             filetypes=filetypes)
        parent.wait_window(dlg)
        return dlg.result_path

    @staticmethod
    def ask_open_filename(parent, initial_dir=None, filetypes=(("Alle Dateien", "*"),)):
        dlg = CTkFileDialog(parent, mode="open", title="Datei öffnen",
                             initial_dir=initial_dir, filetypes=filetypes)
        parent.wait_window(dlg)
        return dlg.result_path


class CTkMsg:
    """Konsistent gestylter Ersatz für tkinter.messagebox (gleicher Look wie
    der Rest der App), mit derselben Aufruf-Signatur wie messagebox
    (showerror/showwarning/showinfo/askyesno), damit bestehende Aufrufstellen
    unverändert bleiben können."""

    _KIND_STYLE = {
        "info": ("ℹ", "#5b9bd5"),
        "warning": ("⚠", "#e6a23c"),
        "error": ("✗", "#EF5350"),
        "question": ("", "#5b9bd5"),
    }

    @staticmethod
    def _show(title, message, kind="info", buttons=("OK",)):
        parent = tk._default_root
        top = ctk.CTkToplevel(parent)
        top.title("")
        if parent is not None:
            top.transient(parent)
        top.grab_set()
        top.resizable(False, False)
        icon, color = CTkMsg._KIND_STYLE.get(kind, ("", "#5b9bd5"))

        frame = ctk.CTkFrame(top, corner_radius=0, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=24, pady=20)

        header_text = f"{icon}  {title}" if icon else title
        ctk.CTkLabel(frame, text=header_text, font=ctk.CTkFont(size=19, weight="bold"),
                     text_color=color, justify="left").pack(anchor="w")
        ctk.CTkLabel(frame, text=message, justify="left", wraplength=400).pack(
            anchor="w", pady=(10, 18))

        result = {"value": buttons[-1]}
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(fill="x")

        def make_click(b):
            def _c():
                result["value"] = b
                top.destroy()
            return _c

        for b in reversed(buttons):
            danger = kind in ("warning", "error") and b in ("Ja", "OK") and len(buttons) > 1
            kwargs = {"fg_color": "#B71C1C", "hover_color": "#8E0000"} if danger else {}
            ctk.CTkButton(btn_frame, text=b, width=100, corner_radius=10,
                          command=make_click(b), **kwargs).pack(side="right", padx=(8, 0))

        top.protocol("WM_DELETE_WINDOW", make_click(buttons[-1]))
        top.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            tw, th = top.winfo_width(), top.winfo_height()
            top.geometry(f"+{px + (pw - tw) // 2}+{py + (ph - th) // 2}")
        except Exception:
            pass
        top.wait_window()
        return result["value"]

    @staticmethod
    def showerror(title, message):
        CTkMsg._show(title, message, kind="error", buttons=("OK",))

    @staticmethod
    def showwarning(title, message):
        CTkMsg._show(title, message, kind="warning", buttons=("OK",))

    @staticmethod
    def showinfo(title, message):
        CTkMsg._show(title, message, kind="info", buttons=("OK",))

    @staticmethod
    def askyesno(title, message, icon=None):
        result = CTkMsg._show(title, message, kind="question", buttons=("Nein", "Ja"))
        return result == "Ja"

    @staticmethod
    def show_success(message):
        """Vereinfachtes Erfolgs-Popup: leere Titelleiste, keine Kopfzeile
        und keine Subline (die die Aussage sonst dreifach wiederholen würden)
        - nur eine einzige blaue Zeile mit der Erfolgsmeldung."""
        parent = tk._default_root
        top = ctk.CTkToplevel(parent)
        top.title("")
        if parent is not None:
            top.transient(parent)
        top.grab_set()
        top.resizable(False, False)

        frame = ctk.CTkFrame(top, corner_radius=0, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=28, pady=22)

        ctk.CTkLabel(frame, text=message, font=ctk.CTkFont(size=14, weight="bold"),
                     text_color="#5b9bd5", justify="left", wraplength=380).pack(anchor="w")

        result = {"value": "OK"}
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(18, 0))

        def _close():
            result["value"] = "OK"
            top.destroy()

        ctk.CTkButton(btn_frame, text="OK", width=100, corner_radius=10,
                      command=_close).pack(side="right")

        top.protocol("WM_DELETE_WINDOW", _close)
        top.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            tw, th = top.winfo_width(), top.winfo_height()
            top.geometry(f"+{px + (pw - tw) // 2}+{py + (ph - th) // 2}")
        except Exception:
            pass
        top.wait_window()
        return result["value"]


class ToolTip:
    """Einfacher Hover-Tooltip für beliebige Tk-/CTk-Widgets, da
    CustomTkinter 6.0.0 keinen eingebauten Tooltip mitbringt."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left",
                 background="#2b2b2b", foreground="#f0f0f0",
                 relief="solid", borderwidth=1, wraplength=380,
                 padx=8, pady=6).pack()

    def _hide(self, event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class AuraPiApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("")
        self.minsize(800, 560)

        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.devices = []
        # Wird von start_backup() vor jedem Lauf zurückgesetzt (.clear()) und
        # von _cancel_backup() gesetzt - backup_device_to_file/gzip_compress_file/
        # run_pkexec prüfen dieses Event bereits selbst und räumen unfertige
        # Dateien automatisch auf (siehe OperationCancelled-Handling).
        self._cancel_event = threading.Event()

        # Scrollbarer Wurzel-Container: sobald der Inhalt bei kleineren
        # Auflösungen/Skalierungsfaktoren (z.B. 125% auf einem entfernt
        # bedienten TV) nicht mehr komplett in die Fensterhöhe passt,
        # erscheint eine dezente, dünne Scrollbar statt dass unten etwas
        # abgeschnitten wird. Passt sie nicht in die Fensterhöhe, bleibt sie
        # unsichtbar - kein optischer Unterschied im Normalfall.
        self._scroll_root = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll_root.pack(fill="both", expand=True)

        self._build_header()

        self.tabview = ctk.CTkTabview(self._scroll_root, corner_radius=14)
        self.tabview.pack(fill="x", padx=16, pady=(16, 8))
        self.backup_tab = self.tabview.add("Backup erstellen")
        self.restore_tab = self.tabview.add("Image zurückspielen")

        self._build_backup_tab()
        self._build_restore_tab()
        self._build_log_area()

        # Responsives Verhalten bei Fenster-Verkleinerung: Icon schrumpft
        # (bis max. 50%), Abstände zwischen den Abschnitten ziehen sich enger
        # zusammen - ohne dass sich etwas überlagert.
        self._is_compact_mode = None
        self._icon_last_container_size = None
        self._responsive_after_id = None

        # Fenstergröße erst JETZT setzen (nachdem der komplette Inhalt gebaut
        # ist, mit standardmäßig zugeklappter CLI). winfo_reqheight() auf dem
        # Toplevel selbst ist bei einem CTkScrollableFrame NICHT verlässlich
        # (der scrollbare Innenbereich kann beliebig größer sein als die
        # Canvas-Anfrage) - stattdessen wird die tatsächliche Inhaltshöhe
        # direkt aus der Canvas-Bounding-Box gelesen. So bleibt das Fenster
        # automatisch kompakt, auch wenn sich der Inhalt später mal ändert
        # (z.B. PiShrink-Zeile ein/aus).
        self.update_idletasks()
        win_w = max(860, self.winfo_reqwidth())
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        bbox = self._scroll_root._parent_canvas.bbox("all")
        content_h = (bbox[3] - bbox[1] + 24) if bbox else 700
        win_h = min(content_h, screen_h - 80)
        pos_x = max(0, (screen_w - win_w) // 2)
        pos_y = max(0, (screen_h - win_h) // 2)
        self.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.update_idletasks()
        self._update_scrollbar_visibility()

        self.bind("<Configure>", self._on_root_configure)
        self.after(200, self._poll_log_queue)
        self.refresh_devices()

    def _on_root_configure(self, event):
        # Nur auf Größenänderungen des Hauptfensters selbst reagieren, nicht
        # auf die vielen <Configure>-Events, die Kind-Widgets ebenfalls durchs
        # Bubbling auslösen.
        if event.widget is not self:
            return
        if self._responsive_after_id is not None:
            self.after_cancel(self._responsive_after_id)
        # Kurzes Debounce, damit beim Ziehen der Fensterkante nicht bei jedem
        # einzelnen Pixel neu skaliert/gerechnet wird.
        self._responsive_after_id = self.after(120, self._apply_responsive_layout)

    def _apply_responsive_layout(self):
        self._responsive_after_id = None
        h = self.winfo_height()

        # Referenzbereich: oberhalb von full_h volle Größe, unterhalb von
        # compact_h maximal kompakt (Icon halbe Größe, Abstände enger).
        full_h, compact_h = 900, 680
        t = (h - compact_h) / (full_h - compact_h)
        t = max(0.0, min(1.0, t))

        icon_full, icon_min = 230, 115
        img_full, img_min = 190, 95
        container_size = int(icon_min + t * (icon_full - icon_min))
        image_size = int(img_min + t * (img_full - img_min))

        # Nur neu zeichnen, wenn sich die Zielgröße spürbar geändert hat
        # (vermeidet unnötiges Neu-Rendern des Bildes bei Mini-Schritten).
        if (self._icon_last_container_size is None or
                abs(container_size - self._icon_last_container_size) >= 4):
            self._icon_last_container_size = container_size
            self.icon_container.configure(width=container_size, height=container_size)
            restore_container = getattr(self, "icon_container_restore", None)
            if restore_container is not None:
                restore_container.configure(width=container_size, height=container_size)
            if self._icon_anim_job is not None:
                # Backup/Restore läuft gerade und die Animation dreht sich -
                # NICHT auf Still zurückfallen, sondern die laufende Animation
                # nur auf die neue Größe umskalieren (ohne Unterbrechung).
                self._resize_icon_animation(image_size)
            else:
                self.set_icon_still(size=image_size)

        is_compact = t < 0.5
        if is_compact != self._is_compact_mode:
            self._is_compact_mode = is_compact
            self._apply_compact_paddings(is_compact)

        self._update_scrollbar_visibility()

    def _update_scrollbar_visibility(self):
        """Blendet die Scrollbar des Wurzel-Containers nur ein, wenn der
        Inhalt tatsächlich nicht in die aktuelle Fensterhöhe passt (z.B. bei
        aufgeklappter CLI auf kleineren Auflösungen/Skalierungsfaktoren) -
        CustomTkinters CTkScrollableFrame zeigt die Scrollbar sonst immer an,
        unabhängig davon ob überhaupt gescrollt werden kann."""
        canvas = self._scroll_root._parent_canvas
        bbox = canvas.bbox("all")
        if not bbox:
            return
        content_h = bbox[3] - bbox[1]
        canvas_h = canvas.winfo_height()
        scrollbar = self._scroll_root._scrollbar
        if content_h > canvas_h + 2:
            scrollbar.grid()
        else:
            scrollbar.grid_remove()

    def _apply_compact_paddings(self, compact):
        """Zieht die Abstände zwischen den Hauptabschnitten enger zusammen,
        wenn das Fenster verkleinert wird (kein Überlagern, nur weniger
        Leerraum) - bzw. stellt die normalen Abstände wieder her."""
        self.tabview.pack_configure(pady=(8, 4) if compact else (16, 8))
        self._modes_frame.grid_configure(pady=(8, 3) if compact else (16, 6))
        self._action_frame.grid_configure(pady=(8, 4) if compact else (18, 10))
        restore_action = getattr(self, "_action_frame_restore", None)
        if restore_action is not None:
            restore_action.grid_configure(pady=(8, 4) if compact else (18, 10))
        self._log_frame.pack_configure(pady=(4, 8) if compact else (8, 16))
        self._quelle_label.grid_configure(pady=(3, 3) if compact else (8, 8))
        self._zieldatei_label.grid_configure(pady=(3, 3) if compact else (8, 8))

    # ---------------- Header (Wortmarke) ----------------

    def _build_header(self):
        """Titelbereich mit der AuraPi-Wortmarke, im dunklen Navy-Ton der
        App (identisch zum Fensterhintergrund, siehe aurapi_navy_theme.json)."""
        header = ctk.CTkFrame(self._scroll_root, corner_radius=0,
                               fg_color=("#c9d4e8", "#1a1a2e"))
        header.pack(fill="x", side="top")

        try:
            wm_img = _decode_embedded_image(_WORDMARK_PNG_B64)
            disp_w = 148  # Breite der Wortmarke im Header
            disp_h = int(disp_w * wm_img.height / wm_img.width)
            self._wordmark_ctkimg = ctk.CTkImage(light_image=wm_img, dark_image=wm_img,
                                                  size=(disp_w, disp_h))
            ctk.CTkLabel(header, image=self._wordmark_ctkimg, text="").pack(pady=(4, 3))
        except Exception as e:
            # Fallback, z.B. wenn PIL.ImageTk auf diesem System nicht
            # funktioniert (Pillow ohne passende Tcl/Tk-Anbindung installiert
            # - siehe README/Installationshinweise). App bleibt so nutzbar,
            # nur ohne Grafik-Wortmarke.
            print(f"[AuraPi] Konnte Wortmarke nicht als Bild laden ({e}). "
                  f"Zeige Text-Fallback. Siehe Installationshinweise zu "
                  f"PIL.ImageTk.")
            ctk.CTkLabel(header, text="AuraPi",
                         font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(4, 3))

    # ---------------- Backup Tab ----------------

    def _build_backup_tab(self):
        f = self.backup_tab
        f.grid_columnconfigure(1, weight=1)
        pad = {"padx": 14, "pady": 8}

        self._quelle_label = ctk.CTkLabel(f, text="Quelle (SD-Karte / USB-Laufwerk)",
                     font=ctk.CTkFont(weight="bold"))
        self._quelle_label.grid(row=0, column=0, sticky="w", **pad)
        self.src_menu = ctk.CTkComboBox(f, values=["Keine Geräte gefunden"], width=380,
                                         corner_radius=10, state="readonly",
                                         fg_color="#191a2e", border_color="#3a7ebf",
                                         border_width=2, text_color="white",
                                         button_color="#3a7ebf", button_hover_color="#2d6294",
                                         dropdown_fg_color="#22223c", dropdown_text_color="white",
                                         dropdown_hover_color="#2a2a44")
        self.src_menu.grid(row=1, column=0, columnspan=2, sticky="we", padx=14)
        ctk.CTkButton(f, text="Aktualisieren", width=140, corner_radius=10,
                      command=self.refresh_devices).grid(row=1, column=2, padx=14)

        ctk.CTkLabel(f, text="Nur Geräte mit Seriennummer und stabiler by-id-Kennung werden angezeigt.",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 10))

        self._zieldatei_label = ctk.CTkLabel(f, text="Zieldatei", font=ctk.CTkFont(weight="bold"))
        self._zieldatei_label.grid(row=3, column=0, sticky="w", **pad)
        self.dest_entry = ctk.CTkEntry(f, corner_radius=10, fg_color="#191a2e",
                                        border_color="#3a7ebf", border_width=2)
        self.dest_entry.insert(0, os.path.expanduser("~/sdcard-backup.img.gz"))
        self.dest_entry.grid(row=4, column=0, columnspan=2, sticky="we", padx=14)
        ctk.CTkButton(f, text="Durchsuchen…", width=140, corner_radius=10,
                      command=self._choose_dest).grid(row=4, column=2, padx=14)

        modes = ctk.CTkFrame(f, corner_radius=14)
        self._modes_frame = modes
        modes.grid(row=5, column=0, columnspan=3, sticky="we", padx=14, pady=(16, 6))
        ctk.CTkLabel(modes, text="Art des Backups", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=14, pady=(10, 4))

        modes_row = ctk.CTkFrame(modes, fg_color="transparent")
        modes_row.pack(anchor="w", fill="x", padx=18, pady=(0, 12))

        self.backup_mode = tk.StringVar(value="rpi")
        rb_rpi = ctk.CTkRadioButton(modes_row, text="Raspberry Pi OS",
                                     variable=self.backup_mode, value="rpi")
        rb_rpi.pack(side="left", padx=(0, 32), pady=6)
        ToolTip(rb_rpi, "Maximal verkleinert über PiShrink. Nur für Layouts mit "
                        "FAT-Boot-Partition und ext4-Root-Partition geeignet "
                        "(Standard bei Raspberry Pi OS) — bricht bei anderen "
                        "Layouts kontrolliert mit Fehlermeldung ab.")

        rb_other = ctk.CTkRadioButton(modes_row, text="Other OS",
                                       variable=self.backup_mode, value="other")
        rb_other.pack(side="left", padx=(0, 32), pady=6)
        ToolTip(rb_other, "Kopiert alle Partitionen unverändert bis zum Ende der "
                          "letzten Partition. Dateisystemunabhängig, aber nicht "
                          "für jedes Boot-/Partitionierungslayout geeignet — "
                          "bei GPT-Datenträgern nicht verfügbar (Backup-GPT am "
                          "Geräteende würde sonst fehlen).")

        rb_full = ctk.CTkRadioButton(modes_row, text="Vollständiges Abbild",
                                      variable=self.backup_mode, value="full")
        rb_full.pack(side="left", pady=6)
        ToolTip(rb_full, "Kopiert die komplette Karte 1:1, inklusive ungenutztem "
                         "Speicher. Funktioniert garantiert mit jedem Layout — "
                         "auch GPT, UBI/UBIFS oder speziellen Bootloadern. "
                         "Größte Datei, aber die sicherste Wahl bei unbekanntem "
                         "oder exotischem System.")

        self._pishrink_status_row = ctk.CTkFrame(f, fg_color="transparent")
        status_row = self._pishrink_status_row
        status_row.grid(row=6, column=0, columnspan=3, sticky="we", padx=14, pady=(4, 0))
        status_row.grid_columnconfigure(0, weight=1)
        self.pishrink_status = ctk.CTkLabel(status_row, text="", anchor="w",
                                             font=ctk.CTkFont(size=12))
        self.pishrink_status.grid(row=0, column=0, sticky="we")
        ctk.CTkButton(status_row, text="Erneut prüfen", width=140, corner_radius=10,
                      command=self._check_pishrink).grid(row=0, column=1, padx=(10, 0))
        self._pishrink_note_label = ctk.CTkLabel(
            f, text="(nur für Modus \"Raspberry Pi OS\" nötig)",
            text_color="gray60", font=ctk.CTkFont(size=11))
        self._pishrink_note_label.grid(
            row=7, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 4))

        action_frame = ctk.CTkFrame(f, fg_color="transparent")
        self._action_frame = action_frame
        # Fester linker Abstand (statt zentriert) - garantiert, dass Icon +
        # Button auf Backup- und Restore-Tab exakt an derselben X-Position
        # stehen, auch wenn beide Tab-Frames intern leicht unterschiedliche
        # Spaltenbreiten hätten. _ACTION_FRAME_LEFT_PADX ist die gemeinsame
        # Referenz für beide Tabs (siehe _build_restore_tab).
        action_frame.grid(row=8, column=0, columnspan=3, sticky="w",
                           padx=(_ACTION_FRAME_LEFT_PADX, 0), pady=(18, 10))

        # Icon-Container: zeigt aktuell nur das unanimierte Still-SVG/PNG.
        # Technisch bereits so angelegt, dass später eine animierte Lotus-
        # Sequenz (GIF oder animiertes SVG, läuft während des Backups) in
        # denselben Container eingesetzt werden kann — siehe set_icon_still()
        # und set_icon_animated_gif() weiter unten.
        self.icon_container = ctk.CTkFrame(action_frame, width=230, height=230,
                                            corner_radius=24,
                                            fg_color=("#c9d4e8", "#1a1a2e"))
        self.icon_container.pack(side="left", padx=(0, 28))
        self.icon_container.pack_propagate(False)

        self.icon_label = ctk.CTkLabel(self.icon_container, text="", image=None)
        self.icon_label.pack(expand=True)
        self._icon_still_ctkimg = None
        self._icon_anim_job = None
        self.set_icon_still()

        self.backup_start_btn = ctk.CTkButton(action_frame, text="Backup starten", height=40,
                      width=180, corner_radius=12, font=ctk.CTkFont(weight="bold"),
                      command=self.start_backup)
        self.backup_start_btn.pack(side="left")
        self._default_btn_color = self.backup_start_btn.cget("fg_color")
        self._default_btn_hover_color = self.backup_start_btn.cget("hover_color")

        self._check_pishrink()

    def set_icon_still(self, size=None):
        """Zeigt das unanimierte Still-Icon im Icon-Container (Zustand außerhalb
        eines laufenden Backups/Restores, auch der Zustand direkt nach
        erfolgreichem Abschluss). Ohne size-Angabe wird die zuletzt bekannte
        responsive Größe beibehalten, statt auf einen festen Standardwert
        zurückzuspringen - wichtig, damit die Größe nach einem Fenster-
        Resize konsistent bleibt, auch über Backup-Start/Abbruch hinweg."""
        if size is None:
            size = getattr(self, "_icon_current_size", 190)
        if self._icon_anim_job is not None:
            self.after_cancel(self._icon_anim_job)
            self._icon_anim_job = None
        try:
            still_img = _decode_embedded_image(_ICON_STILL_B64).convert("RGBA")
            self._icon_current_size = size
            self._icon_still_ctkimg = ctk.CTkImage(light_image=still_img, dark_image=still_img,
                                                    size=(size, size))
            self.icon_label.configure(image=self._icon_still_ctkimg)
            restore_label = getattr(self, "icon_label_restore", None)
            if restore_label is not None:
                restore_label.configure(image=self._icon_still_ctkimg)
        except Exception as e:
            print(f"[AuraPi] Konnte Icon nicht als Bild laden ({e}). "
                  f"Zeige Text-Fallback. Siehe Installationshinweise zu "
                  f"PIL.ImageTk.")
            self.icon_label.configure(image=None, text="AuraPi")
            restore_label = getattr(self, "icon_label_restore", None)
            if restore_label is not None:
                restore_label.configure(image=None, text="AuraPi")

    def _ensure_icon_anim_frames_decoded(self):
        """Dekodiert die eingebettete Animations-GIF einmalig in eine Liste
        aus (PIL-Image, Anzeigedauer-in-ms) - danach nur noch aus dem Cache
        gelesen, egal wie oft die Animation gestartet/gestoppt wird."""
        if getattr(self, "_icon_anim_raw_frames", None) is not None:
            return
        anim_gif = _decode_embedded_image(_ICON_ANIM_B64)
        frames = []
        for frame in ImageSequence.Iterator(anim_gif):
            frames.append((frame.convert("RGBA"),
                            frame.info.get("duration", 80)))
        self._icon_anim_raw_frames = frames

    def _resize_icon_animation(self, size):
        """Baut die gecachten Animations-Frames auf eine neue Zielgröße um,
        OHNE die laufende Animation zu unterbrechen oder neu zu starten -
        der bereits laufende after()-Loop liest self._icon_anim_ctk_frames
        beim nächsten Tick einfach in der neuen Größe. Wird sowohl beim
        Animationsstart als auch bei einem Fenster-Resize während einer
        laufenden Animation verwendet."""
        self._ensure_icon_anim_frames_decoded()
        if (getattr(self, "_icon_anim_ctk_frames", None) is None or
                getattr(self, "_icon_anim_ctk_size", None) != size):
            self._icon_anim_ctk_frames = [
                ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
                for img, _duration in self._icon_anim_raw_frames
            ]
            self._icon_anim_ctk_size = size
        self._icon_current_size = size

    def set_icon_animated_gif(self, size=None):
        """Spielt die animierte Lotus-Sequenz im Icon-Container ab (während
        ein Backup/Restore läuft). Läuft in einer Endlosschleife, bis
        set_icon_still() wieder zurückschaltet."""
        if size is None:
            size = getattr(self, "_icon_current_size", 190)
        self._resize_icon_animation(size)

        if self._icon_anim_job is not None:
            self.after_cancel(self._icon_anim_job)
        self._icon_anim_index = 0
        self._advance_icon_animation()

    def _advance_icon_animation(self):
        frames = self._icon_anim_ctk_frames
        durations = [d for _img, d in self._icon_anim_raw_frames]
        idx = self._icon_anim_index % len(frames)
        self.icon_label.configure(image=frames[idx])
        restore_label = getattr(self, "icon_label_restore", None)
        if restore_label is not None:
            restore_label.configure(image=frames[idx])
        self._icon_anim_index = idx + 1
        self._icon_anim_job = self.after(max(20, durations[idx]),
                                          self._advance_icon_animation)

    def _check_pishrink(self):
        """Prüft PiShrink automatisch (u.a. beim Programmstart). Die
        Status-/Erneut-prüfen-Zeile ist nur sichtbar, wenn PiShrink NICHT
        verifiziert werden konnte - im Normalfall (einmal korrekt installiert)
        bleibt sie unauffällig ausgeblendet, um die Oberfläche im täglichen
        Gebrauch nicht mit einer Prüfung zu überladen, die dann ohnehin nie
        fehlschlägt. Die eigentliche sicherheitsrelevante Prüfung bleibt
        davon unberührt: start_backup() verifiziert im Modus 'Raspberry Pi OS'
        vor jedem Lauf ohnehin erneut, unabhängig vom UI-Zustand hier."""
        ok, info = verify_pishrink()
        if ok:
            self.pishrink_status.configure(
                text=f"✓ PiShrink verifiziert (Hash & Root-Besitz OK): {info}",
                text_color="#4CAF50")
            self._pishrink_status_row.grid_remove()
            self._pishrink_note_label.grid_remove()
        else:
            self.pishrink_status.configure(
                text=f"✗ PiShrink nicht verifiziert ({info}) — siehe Installationsanleitung.",
                text_color="#EF5350")
            self._pishrink_status_row.grid()
            self._pishrink_note_label.grid()

    def _choose_dest(self):
        ext = ".img" if self.backup_mode.get() == "full" else ".img.gz"
        path = CTkFileDialog.ask_save_filename(
            self, initial_dir=os.path.dirname(self.dest_entry.get()) or os.path.expanduser("~"),
            initial_file="sdcard-backup" + ext,
            filetypes=[("Image-Dateien", "*.img *.img.gz"), ("Alle Dateien", "*")])
        if path:
            self.dest_entry.delete(0, tk.END)
            self.dest_entry.insert(0, path)

    def start_backup(self):
        if self.worker_thread and self.worker_thread.is_alive():
            CTkMsg.showwarning("Bitte warten", "Es läuft bereits ein Vorgang.")
            return
        idx = self._selected_index(self.src_menu, self.devices)
        if idx is None:
            CTkMsg.showerror("Keine Quelle", "Bitte zuerst ein Quell-Laufwerk auswählen.")
            return
        dev_info = dict(self.devices[idx])
        dest = self.dest_entry.get().strip()
        if not dest:
            CTkMsg.showerror("Kein Ziel", "Bitte einen Zieldateinamen angeben.")
            return
        if dest.startswith("/dev/"):
            CTkMsg.showerror("Ungültiges Ziel", "Das Ziel darf kein Gerätepfad (/dev/...) sein.")
            return
        dest_abs = os.path.abspath(dest)
        if os.path.islink(dest_abs):
            CTkMsg.showerror("Ungültiges Ziel", "Zielpfad ist ein Symlink — abgelehnt.")
            return

        mode = self.backup_mode.get()
        if mode in ("rpi", "other") and not dest_abs.endswith(".img.gz"):
            CTkMsg.showerror(
                "Falsche Dateiendung",
                "Die Modi 'Raspberry Pi OS' und 'Other OS' erzeugen komprimierte "
                "Dateien und benötigen die Endung .img.gz.")
            return
        if mode == "full" and dest_abs.endswith(".gz"):
            CTkMsg.showerror(
                "Falsche Dateiendung",
                "Der Modus 'Vollständiges Abbild' erzeugt keine komprimierte "
                "Datei. Bitte eine Endung ohne .gz verwenden (z. B. .img).")
            return

        overwrite = False
        if os.path.exists(dest_abs):
            overwrite = CTkMsg.askyesno("Datei existiert bereits",
                                             f"{dest_abs} existiert bereits. Überschreiben?")
            if not overwrite:
                return

        if mode == "rpi":
            ok, info = verify_pishrink()
            if not ok:
                CTkMsg.showerror("PiShrink nicht verifiziert", f"{info}\n\n{PISHRINK_INSTALL_HINT}")
                return

        mode_label = {"rpi": "Raspberry Pi OS", "other": "Other OS", "full": "Vollständiges Abbild"}[mode]
        detail = (f"Modell: {dev_info['model'] or '?'}\n"
                  f"Seriennummer: {dev_info['serial']}\n"
                  f"Größe: {human_size(dev_info['size_bytes'])}\n"
                  f"Gerät: {dev_info['raw_device']}")
        if not CTkMsg.askyesno(
                "Backup bestätigen",
                f"Quelle:\n{detail}\n\n"
                f"Ziel: {dest_abs}\n\n"
                f"Modus: {mode_label}\n\n"
                "Fortfahren?"):
            return

        self._cancel_event.clear()
        self._set_busy(True, kind="backup")
        self.worker_thread = threading.Thread(target=self._backup_worker,
                                               args=(dev_info, dest_abs, mode, overwrite), daemon=True)
        self.worker_thread.start()

    def _backup_worker(self, dev_info, dest, mode, overwrite):
        """Dünner, abgesicherter Wrapper: fängt jede unerwartete Ausnahme im
        Hintergrundthread ab, damit die GUI nie dauerhaft auf 'läuft' hängen
        bleibt, ohne dass der Nutzer einen Fehler sieht. Baut außerdem
        EINMALIG die sudo-Sitzung auf und hält sie über die gesamte
        Vorgangsdauer aktiv (Keep-Alive), damit nicht mehrfach nach dem
        Passwort gefragt wird."""
        self._log("Fordere Root-Rechte an (einmalig für diesen Vorgang) …")
        try:
            keepalive_stop = prime_sudo_session()
        except RuntimeError as e:
            self._finish("Backup", -1, str(e))
            return
        try:
            self._backup_worker_impl(dev_info, dest, mode, overwrite)
        except Exception as e:
            self._log(f"Unerwarteter Fehler: {e!r}")
            self._finish("Backup", -1, str(e))
        finally:
            keepalive_stop.set()

    def _backup_worker_impl(self, dev_info, dest, mode, overwrite):
        device = dev_info["device"]
        self._log(f"=== Backup gestartet: {dev_info['raw_device']} "
                   f"({dev_info['model']}, S/N {dev_info['serial']}) -> {dest} ===")

        ok, reason = reverify_device(dev_info)
        if not ok:
            self._finish("Backup", -1, reason)
            return

        total = dev_info["size_bytes"]
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

        raw_path = dest
        for suf in (".img.gz", ".gz"):
            if raw_path.endswith(suf):
                raw_path = raw_path[: -len(suf)]
                break
        if not raw_path.endswith(".img"):
            raw_path += ".img"
        raw_tmp = raw_path + ".raw.img"

        # Ab hier kann ein Klick auf "Backup abbrechen" jederzeit eine
        # OperationCancelled auslösen (aus backup_device_to_file/
        # gzip_compress_file/run_pkexec heraus, sobald self._cancel_event
        # gesetzt wird). Der except-Block unten räumt dann alle bislang
        # erzeugten unfertigen Dateien auf, statt Datenmüll zurückzulassen.
        try:
            # --- Schritt A: parted-basierte Prüfungen VOR dem Aushängen. ---
            # parted kann eine ganz normal gemountete Partition i. d. R. sofort
            # lesen (kein Lock-Wettlauf mit dem Auto-Mount-Dienst); erst NACH
            # dem Aushängen kann es kurzzeitig zu Konflikten mit udisksd/GVFS
            # kommen, wie in der Praxis beobachtet. Deshalb hier zuerst prüfen,
            # dann erst aushängen.
            limit = total  # für "other" ggf. unten überschrieben
            if mode == "other":
                self._log("Ermittle Partitionstabelle und Ende der letzten Partition "
                           "(Kompakt-Methode) …")
                limit, table_type = compact_copy_size(device, total)
                self._log(f"Erkannte Partitionstabelle: {table_type!r}")
                if table_type != "msdos":
                    self._finish("Backup", -1,
                                 f"Kompakt-Modus ist nur für MBR-Partitionstabellen ('msdos') "
                                 f"freigegeben, erkannt wurde: {table_type!r}. Bei GPT fehlt "
                                 f"sonst das sekundäre GPT-Backup am Geräteende, bei unbekanntem "
                                 f"Layout ist die Sicherheit nicht gewährleistet. Bitte "
                                 f"'Vollständiges Abbild' für dieses Gerät verwenden.")
                    return
            elif mode == "rpi":
                self._log("Prüfe, ob das Layout zu PiShrink passt (MBR + ext2/3/4-Root) …")
                layout_ok, layout_reason = check_rpi_compatible_layout(device)
                self._log(layout_reason)
                if not layout_ok:
                    self._finish("Backup", -1,
                                 f"Layout nicht mit dem 'Raspberry Pi OS'-Modus kompatibel: "
                                 f"{layout_reason} Bitte 'Other OS' oder 'Vollständiges Abbild' "
                                 f"verwenden.")
                    return

            # --- Schritt B: aushängen. Für 'rpi'/'other' fail closed - beide
            # brauchen einen sauberen, unveränderten Zustand für dd bzw. die
            # anschließende PiShrink-Verarbeitung. Für 'full' reicht ein
            # Warnhinweis: der Stillstands-Watchdog im Lesevorgang fängt ein
            # tatsächliches Hängenbleiben ohnehin ab. ---
            self._log("Hänge eventuell gemountete Partitionen der Quelle aus "
                       "(vermeidet Blockaden bei manchen Kartenlesern) …")
            unmounted_cleanly = unmount_all_partitions(device, self._log)
            if not unmounted_cleanly:
                if mode in ("rpi", "other"):
                    self._finish("Backup", -1,
                                 "Nicht alle Partitionen der Quelle konnten ausgehängt werden. "
                                 "Bitte Dateimanager/andere Programme schließen, die die Karte "
                                 "gerade offen haben könnten, dann 'Aktualisieren' klicken und "
                                 "erneut versuchen.")
                    return
                self._log("Warnung: Nicht alle Partitionen konnten ausgehängt werden — "
                           "falls das Backup gleich hängen bleibt, wird es nach 45s "
                           "automatisch mit einer klaren Meldung abgebrochen.")

            # Kurze Stabilisierung nach dem Aushängen, bevor wir das Gerät
            # erneut anfassen - reduziert die beobachtete Race Condition mit
            # dem Auto-Mount-Dienst zusätzlich zu den Retries in
            # unmount_all_partitions() selbst.
            udevadm_settle()
            time.sleep(1.0)

            ok, reason = reverify_device(dev_info)
            if not ok:
                self._finish("Backup", -1, f"Nach dem Aushängen: {reason}")
                return

            if mode == "full":
                try:
                    rc, digest = backup_device_to_file(device, dest, self._log, self._progress,
                                                        total, overwrite,
                                                        cancel_event=self._cancel_event)
                except SafetyError as e:
                    self._finish("Backup", -1, str(e))
                    return
                if rc == 0:
                    write_sha256_sidecar(dest, digest)
                    self._log(f"SHA-256: {digest}")
                self._finish("Backup", rc)
                return

            if mode == "other":
                self._log(f"Kopiere {human_size(limit)} von {human_size(total)} "
                           f"(Rest der Karte ist ungenutzt) …")
                try:
                    rc, _ = backup_device_to_file(device, raw_tmp, self._log, self._progress,
                                                  limit, overwrite=True, limit_bytes=limit,
                                                  cancel_event=self._cancel_event)
                except SafetyError as e:
                    self._finish("Backup", -1, str(e))
                    return
                if rc != 0:
                    self._finish("Backup", rc, "dd ist fehlgeschlagen.")
                    return

                raw_size = os.path.getsize(raw_tmp)
                self._log("Komprimiere Abbild …")
                digest = gzip_compress_file(raw_tmp, dest, self._log, self._progress,
                                            cancel_event=self._cancel_event)

                self._log("Prüfe Integrität des erzeugten Abbilds (Validierungsdurchlauf) …")
                try:
                    validated_size = full_gzip_size(dest)
                except GZIP_ERRORS as e:
                    try:
                        os.remove(dest)
                    except Exception:
                        pass
                    self._finish("Backup", -1, f"Erzeugtes Abbild ist beschädigt: {e}")
                    return
                if validated_size != raw_size:
                    try:
                        os.remove(dest)
                    except Exception:
                        pass
                    self._finish("Backup", -1,
                                 f"Integritätsprüfung fehlgeschlagen: erwartet {raw_size} Bytes, "
                                 f"entpackt {validated_size} Bytes.")
                    return

                write_sha256_sidecar(dest, digest)
                self._log(f"Integrität OK. SHA-256 (komprimierte Datei): {digest}")
                self._finish("Backup", 0)
                return

            # mode == "rpi": PiShrink-Weg (Layout bereits vor dem Aushängen geprüft)
            self._log("Schritt 1/2: Rohes Abbild der Karte lesen (Root liest nur, "
                       "unprivilegiert geschrieben) …")
            try:
                rc, _ = backup_device_to_file(device, raw_tmp, self._log, self._progress, total,
                                              overwrite=True, cancel_event=self._cancel_event)
            except SafetyError as e:
                self._finish("Backup", -1, str(e))
                return
            if rc != 0:
                self._finish("Backup", rc, "dd ist fehlgeschlagen.")
                return

            ok, p_bin = verify_pishrink()
            if not ok:
                self._finish("Backup", -1, f"PiShrink nicht mehr verifizierbar: {p_bin}")
                try:
                    os.remove(raw_tmp)
                except Exception:
                    pass
                return

            self._log("Schritt 2/2: Abbild mit verifiziertem PiShrink verkleinern und komprimieren …")
            self._progress_indeterminate(True, "Shrink läuft …")
            rc = run_pkexec([p_bin, "-z", raw_tmp, raw_path], self._log,
                            cancel_event=self._cancel_event)
            self._progress_indeterminate(False)
            try:
                os.remove(raw_tmp)
            except Exception as e:
                self._log(f"Hinweis: Temporäre Datei {raw_tmp} konnte nicht entfernt werden: {e}")

            produced_gz = raw_path + ".gz"
            if rc == 0 and os.path.exists(produced_gz):
                # PiShrink lief als Root -> die Ausgabedatei gehört root:root.
                # Vor dem unprivilegierten move/remove den Besitz zurückgeben.
                # Rückgabecode wird geprüft - bei Fehlschlag brechen wir ab,
                # statt mit einer root-owned Datei weiterzuarbeiten.
                try:
                    chown = require_bin("chown")
                    r = subprocess.run(privileged([chown, f"{os.getuid()}:{os.getgid()}", produced_gz]),
                                        capture_output=True, text=True)
                except Exception as e:
                    self._finish("Backup", -1,
                                 f"Besitz der PiShrink-Ausgabe konnte nicht geändert werden: {e}")
                    return
                if r.returncode != 0:
                    self._finish("Backup", -1,
                                 f"Besitz der PiShrink-Ausgabe konnte nicht geändert werden: "
                                 f"{r.stderr.strip()}")
                    return

            if rc == 0 and os.path.exists(produced_gz) and produced_gz != dest:
                if os.path.exists(dest):
                    os.remove(dest)
                shutil.move(produced_gz, dest)

            if rc == 0:
                self._log("Prüfe Integrität des PiShrink-Abbilds (Validierungsdurchlauf) …")
                try:
                    full_gzip_size(dest)
                except GZIP_ERRORS as e:
                    self._finish("Backup", -1, f"PiShrink-Ausgabe ist beschädigt: {e}")
                    return
                hasher = hashlib.sha256()
                with open(dest, "rb") as f:
                    for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                        hasher.update(chunk)
                digest = hasher.hexdigest()
                write_sha256_sidecar(dest, digest)
                self._log(f"Integrität OK. SHA-256: {digest}")

            self._finish("Backup", rc)

        except OperationCancelled:
            self._progress_indeterminate(False)
            self._log("Backup abgebrochen - räume unfertige Dateien auf …")
            for p in (raw_tmp, raw_path + ".gz", dest, dest + ".part", dest + ".sha256.part"):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                        self._log(f"Entfernt: {p}")
                except Exception as e:
                    self._log(f"Hinweis: {p} konnte nicht entfernt werden: {e}")
            self._finish("Backup", -1, "Vom Nutzer abgebrochen.")

    # ---------------- Restore Tab ----------------

    @staticmethod
    def _build_warning_box(parent, body_text, achtung_text="Achtung:", icon_size=76,
                            wraplength=560):
        """Baut die 'Achtung'-Warnbox exakt nach Vorlage: links ein
        quadratischer, roter Icon-Block (volle Boxhöhe) mit großem Warn-
        dreieck, rechts daneben 'Achtung:' fett und der Fließtext normal-
        gewichtet direkt daneben. Farben 1:1 aus dem Mockup-Screenshot
        gesampelt: Rahmen/Icon-Block #A82D26, Box-Füllung #370D0B."""
        warn = ctk.CTkFrame(parent, corner_radius=12, fg_color="#370D0B",
                             border_width=2, border_color="#A82D26")

        icon_block = ctk.CTkFrame(warn, corner_radius=10, fg_color="#A82D26",
                                   width=icon_size, height=icon_size)
        icon_block.pack(side="left", padx=(6, 0), pady=6)
        icon_block.pack_propagate(False)
        ctk.CTkLabel(icon_block, text="⚠", font=ctk.CTkFont(size=int(icon_size * 0.42)),
                     text_color="white").pack(expand=True)

        text_row = ctk.CTkFrame(warn, fg_color="transparent")
        text_row.pack(side="left", fill="both", expand=True, padx=(16, 16), pady=10)

        ctk.CTkLabel(text_row, text=achtung_text,
                     font=ctk.CTkFont(size=19, weight="bold"),
                     text_color="white").pack(side="left", anchor="n", padx=(0, 16))
        ctk.CTkLabel(text_row, text=body_text, justify="left", wraplength=wraplength,
                     font=ctk.CTkFont(size=19), text_color="white").pack(
            side="left", anchor="n")

        return warn

    def _build_restore_tab(self):
        f = self.restore_tab
        f.grid_columnconfigure(1, weight=1)
        pad = {"padx": 14, "pady": 8}

        ctk.CTkLabel(f, text="Image-Datei", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", **pad)
        self.img_entry = ctk.CTkEntry(f, corner_radius=10, fg_color="#191a2e",
                                       border_color="#3a7ebf", border_width=2)
        self.img_entry.grid(row=1, column=0, columnspan=2, sticky="we", padx=14)
        ctk.CTkButton(f, text="Durchsuchen…", width=140, corner_radius=10,
                      command=self._choose_image).grid(row=1, column=2, padx=14)

        ctk.CTkLabel(f, text="Ziel-Laufwerk", font=ctk.CTkFont(weight="bold")).grid(
            row=2, column=0, sticky="w", padx=14, pady=(16, 8))
        self.dst_menu = ctk.CTkComboBox(f, values=["Keine Geräte gefunden"], width=380,
                                         corner_radius=10, state="readonly",
                                         fg_color="#191a2e", border_color="#3a7ebf",
                                         border_width=2, text_color="white",
                                         button_color="#3a7ebf", button_hover_color="#2d6294",
                                         dropdown_fg_color="#22223c", dropdown_text_color="white",
                                         dropdown_hover_color="#2a2a44")
        self.dst_menu.grid(row=3, column=0, columnspan=2, sticky="we", padx=14)
        ctk.CTkButton(f, text="Aktualisieren", width=140, corner_radius=10,
                      command=self.refresh_devices).grid(row=3, column=2, padx=14)

        warn = self._build_warning_box(
            f,
            body_text="Beim zurückspielen werden ALLE Daten auf dem\n"
                       "Ziellaufwerk  unwiederruflich überschrieben.")
        warn.grid(row=4, column=0, columnspan=3, sticky="we", padx=14, pady=(18, 8))

        # Gleicher fester linker Abstand wie im Backup-Tab (siehe dort) -
        # garantiert identische X-Position von Icon+Button auf beiden Tabs.
        action_frame_restore = ctk.CTkFrame(f, fg_color="transparent")
        self._action_frame_restore = action_frame_restore
        action_frame_restore.grid(row=5, column=0, columnspan=3, sticky="w",
                                   padx=(_ACTION_FRAME_LEFT_PADX, 0), pady=(18, 10))

        self.icon_container_restore = ctk.CTkFrame(action_frame_restore, width=230, height=230,
                                                     corner_radius=24,
                                                     fg_color=("#c9d4e8", "#1a1a2e"))
        self.icon_container_restore.pack(side="left", padx=(0, 28))
        self.icon_container_restore.pack_propagate(False)

        self.icon_label_restore = ctk.CTkLabel(self.icon_container_restore, text="", image=None)
        self.icon_label_restore.pack(expand=True)
        # Direkt mit dem aktuellen Still-/Animations-Zustand synchronisieren,
        # falls z.B. beim Programmstart bereits ein Bild geladen wurde.
        if getattr(self, "_icon_still_ctkimg", None) is not None:
            self.icon_label_restore.configure(image=self._icon_still_ctkimg)

        self.restore_start_btn = ctk.CTkButton(action_frame_restore, text="Restore starten",
                      height=40, width=180, corner_radius=12,
                      fg_color="#B71C1C", hover_color="#8E0000",
                      font=ctk.CTkFont(weight="bold"),
                      command=self.start_restore)
        self.restore_start_btn.pack(side="left")

    def _choose_image(self):
        path = CTkFileDialog.ask_open_filename(
            self, initial_dir=os.path.dirname(self.img_entry.get()) or os.path.expanduser("~"),
            filetypes=[("Image-Dateien", "*.img *.img.gz *.gz"), ("Alle Dateien", "*")])
        if path:
            self.img_entry.delete(0, tk.END)
            self.img_entry.insert(0, path)

    def start_restore(self):
        if self.worker_thread and self.worker_thread.is_alive():
            CTkMsg.showwarning("Bitte warten", "Es läuft bereits ein Vorgang.")
            return
        image = self.img_entry.get().strip()
        idx = self._selected_index(self.dst_menu, self.devices)
        if not image or not os.path.exists(image):
            CTkMsg.showerror("Keine Datei", "Bitte eine gültige Image-Datei auswählen.")
            return
        if not os.access(image, os.R_OK):
            CTkMsg.showerror("Kein Zugriff",
                                  "Du hast selbst kein Leserecht auf diese Datei — "
                                  "das Tool nutzt Root nicht, um das zu umgehen.")
            return
        if idx is None:
            CTkMsg.showerror("Kein Ziel", "Bitte ein Ziel-Laufwerk auswählen.")
            return
        dev_info = dict(self.devices[idx])
        device = dev_info["device"]
        target_real = os.path.realpath(device)

        origin_state, origin_info = image_is_on_device(image, target_real)
        if origin_state == "on":
            CTkMsg.showerror(
                "Ungültige Auswahl",
                f"Die Image-Datei liegt selbst auf dem Ziel-Laufwerk ({origin_info}). "
                "Restore würde die eigene Quelle während des Schreibens zerstören.")
            return
        if origin_state == "unknown":
            CTkMsg.showerror(
                "Herkunft nicht feststellbar",
                f"Es konnte nicht sicher ermittelt werden, ob die Image-Datei auf dem "
                f"Ziel-Laufwerk liegt ({origin_info}). Aus Sicherheitsgründen wird der "
                f"Restore in diesem Fall abgelehnt statt es zu riskieren.")
            return

        is_gz = image.endswith(".gz")
        if not is_gz:
            img_size = os.path.getsize(image)
            if img_size > dev_info["size_bytes"]:
                CTkMsg.showerror(
                    "Zielgerät zu klein",
                    f"Image ist {human_size(img_size)} groß, "
                    f"Ziel hat nur {human_size(dev_info['size_bytes'])}.")
                return

        detail = (f"Modell: {dev_info['model'] or '?'}\n"
                  f"Seriennummer: {dev_info['serial']}\n"
                  f"Größe: {human_size(dev_info['size_bytes'])}\n"
                  f"Gerät: {dev_info['raw_device']}")
        if not self._confirm_restore_dialog(image, dev_info, detail):
            return

        self._set_busy(True, kind="restore")
        self.worker_thread = threading.Thread(target=self._restore_worker,
                                               args=(image, dev_info), daemon=True)
        self.worker_thread.start()

    def _confirm_restore_dialog(self, image, dev_info, detail):
        """Ein einziges Fenster für die komplette Restore-Bestätigung:
        Zusammenfassung, Warnung und Tipp-Bestätigung in einem statt in
        zwei getrennten Popups."""
        device = dev_info["raw_device"]
        top = ctk.CTkToplevel(self)
        top.title("Restore bestätigen")
        top.geometry("520x420")
        top.transient(self)
        top.grab_set()
        result = {"ok": False}

        frame = ctk.CTkFrame(top, corner_radius=0, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=22, pady=20)

        ctk.CTkLabel(frame, text="Restore bestätigen", font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor="w")

        info_text = f"Image:\n{image}\n\nZiel:\n{detail}"
        ctk.CTkLabel(frame, text=info_text, justify="left", wraplength=460).pack(
            anchor="w", pady=(12, 12))

        warn_box = self._build_warning_box(
            frame,
            body_text="ALLE Daten auf diesem Laufwerk werden GELÖSCHT.\n"
                       "Diesen Vorgang kann man NICHT rückgängig machen.",
            icon_size=60, wraplength=260)
        warn_box.pack(fill="x", pady=(0, 14))

        ctk.CTkLabel(frame, text=f"Zur Bestätigung genau eintippen: {device}",
                     justify="left", wraplength=460).pack(anchor="w", pady=(0, 6))
        entry = ctk.CTkEntry(frame, width=460, corner_radius=10)
        entry.pack(pady=(0, 4))
        error_label = ctk.CTkLabel(frame, text="", text_color="#EF5350")
        error_label.pack(anchor="w")

        def confirm():
            if entry.get().strip() == device:
                result["ok"] = True
                top.destroy()
            else:
                error_label.configure(text="Eingabe stimmt nicht mit dem Gerätenamen überein.")

        def cancel():
            result["ok"] = False
            top.destroy()

        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(14, 0))
        ctk.CTkButton(btn_frame, text="Abbrechen", corner_radius=10, fg_color="gray30",
                      command=cancel).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn_frame, text="Restore starten", corner_radius=10,
                      fg_color="#B71C1C", hover_color="#8E0000",
                      command=confirm).pack(side="right")

        top.protocol("WM_DELETE_WINDOW", cancel)
        entry.bind("<Return>", lambda e: confirm())
        entry.focus_set()
        self.wait_window(top)
        return result["ok"]

    def _restore_worker(self, image, dev_info):
        """Abgesicherter Wrapper, analog zu _backup_worker, inkl. einmaliger
        sudo-Sitzung + Keep-Alive für die gesamte Vorgangsdauer."""
        self._log("Fordere Root-Rechte an (einmalig für diesen Vorgang) …")
        try:
            keepalive_stop = prime_sudo_session()
        except RuntimeError as e:
            self._finish("Restore", -1, str(e))
            return
        try:
            self._restore_worker_impl(image, dev_info)
        except Exception as e:
            self._log(f"Unerwarteter Fehler: {e!r}")
            self._finish("Restore", -1, str(e))
        finally:
            keepalive_stop.set()

    def _restore_worker_impl(self, image, dev_info):
        device = dev_info["device"]
        self._log(f"=== Restore gestartet: {image} -> {dev_info['raw_device']} "
                   f"({dev_info['model']}, S/N {dev_info['serial']}) ===")

        ok, reason = reverify_device(dev_info)
        if not ok:
            self._finish("Restore", -1, reason)
            return

        self._log("Hänge eventuell gemountete Partitionen des Ziels aus …")
        if not unmount_all_partitions(device, self._log):
            self._finish("Restore", -1,
                         "Mindestens eine Partition konnte nicht ausgehängt werden.")
            return

        ok, reason = reverify_device(dev_info)
        if not ok:
            self._finish("Restore", -1, f"Nach dem Aushängen: {reason}")
            return

        # Konsistenz mit der Geräte-Reverifikation: auch die Image-Herkunft
        # wird nach dem Aushängen erneut geprüft, nicht nur einmalig vor
        # dem Bestätigungsdialog in start_restore().
        origin_state, origin_info = image_is_on_device(image, os.path.realpath(device))
        if origin_state != "off":
            self._finish("Restore", -1,
                         f"Image-Herkunft nach dem Aushängen nicht mehr sicher als "
                         f"unabhängig vom Ziel bestätigbar ({origin_state}: {origin_info}).")
            return

        try:
            dd = require_bin("dd")
        except RuntimeError as e:
            self._finish("Restore", -1, str(e))
            return

        if image.endswith(".gz"):
            self._log("Validiere Image-Größe (vollständiger Durchlauf 1/2) …")
            try:
                total = full_gzip_size(
                    image, progress_cb=lambda n: self._progress(
                        min(50.0, n * 50.0 / max(dev_info["size_bytes"], 1)),
                        f"Validierung: {n} Bytes geprüft"),
                    max_size=dev_info["size_bytes"])
            except SafetyError as e:
                self._finish("Restore", -1, str(e))
                return
            except GZIP_ERRORS as e:
                self._finish("Restore", -1, f"Image konnte nicht gelesen/entpackt werden: {e}")
                return
            if total > dev_info["size_bytes"]:
                self._finish("Restore", -1,
                             f"Entpacktes Image ist {human_size(total)} groß, "
                             f"Ziel hat nur {human_size(dev_info['size_bytes'])}.")
                return
            self._log(f"Validierung OK: {human_size(total)}. Schreibe Durchlauf 2/2 …")

            def chunks():
                with gzip.open(image, "rb") as fh:
                    while True:
                        data = fh.read(4 * 1024 * 1024)
                        if not data:
                            break
                        yield data

            rc = run_pkexec_stdin_pipe(
                [dd, f"of={device}", "bs=4M", "conv=fsync,notrunc"],
                chunks, total, self._log,
                lambda pct, msg: self._progress(50 + pct / 2, msg))
        else:
            total = os.path.getsize(image)
            args = [dd, f"if={image}", f"of={device}", "bs=4M",
                    "status=progress", "conv=fsync,notrunc"]
            rc = run_pkexec(args, self._log, self._progress, total)

        self._finish("Restore", rc)

    # ---------------- Log / Fortschritt ----------------

    def _build_log_area(self):
        f = ctk.CTkFrame(self._scroll_root, corner_radius=14)
        f.pack(fill="x", padx=16, pady=(8, 16))
        self._log_frame = f

        self.progress = ctk.CTkProgressBar(f, corner_radius=8)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=14, pady=(14, 6))

        self.status_var = tk.StringVar(value="Bereit.")
        ctk.CTkLabel(f, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=14)

        # Kleiner Auf-/Zuklapp-Schalter für die rohe Shell-Ausgabe - im
        # Normalbetrieb braucht man das CLI-Log selten, blendet es aber bei
        # Bedarf (Fehlersuche) mit einem Klick wieder ein. Startet zugeklappt,
        # damit das Fenster beim Programmstart kompakt und aufgeräumt wirkt.
        toggle_row = ctk.CTkFrame(f, fg_color="transparent")
        toggle_row.pack(fill="x", padx=14, pady=(6, 8))
        self._cli_visible = False
        self._cli_toggle_btn = ctk.CTkButton(
            toggle_row, text="CLI ▸", width=64, height=22, corner_radius=6,
            fg_color="transparent", hover_color=("#b9c6e0", "#242440"),
            text_color=("#5b6584", "#9aa8c8"), font=ctk.CTkFont(size=11),
            command=self._toggle_cli_log)
        self._cli_toggle_btn.pack(side="left")

        # Feste Höhe statt expand=True: innerhalb des scrollbaren Wurzel-
        # Containers sizt sich der Inhalt auf seine natürliche Größe, "expand"
        # hat dort keine Wirkung mehr. Eine feste Höhe hält das Log trotzdem
        # gut lesbar; bei Bedarf sorgt die äußere Scrollbar für den Rest.
        self._log_text_pack_opts = dict(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_text = ctk.CTkTextbox(f, corner_radius=10, state="disabled",
                                        wrap="word", height=170,
                                        font=ctk.CTkFont(family="monospace", size=12))
        # Bewusst NICHT gepackt - bleibt unsichtbar, bis der Nutzer per CLI-
        # Toggle aufklappt (siehe _toggle_cli_log).

    def _toggle_cli_log(self):
        self._cli_visible = not self._cli_visible
        if self._cli_visible:
            self.log_text.pack(**self._log_text_pack_opts)
            self._cli_toggle_btn.configure(text="CLI ▾")
        else:
            self.log_text.pack_forget()
            self._cli_toggle_btn.configure(text="CLI ▸")
        self.update_idletasks()
        self._update_scrollbar_visibility()

    def _log(self, msg):
        self.log_queue.put(("log", msg))

    def _progress(self, pct, msg):
        self.log_queue.put(("progress", (pct, msg)))

    def _progress_indeterminate(self, on, msg=""):
        """Schaltet den Fortschrittsbalken auf pulsierende Animation um
        (für Phasen ohne Byte-genaue Fortschrittsangabe, z. B. PiShrink)
        oder zurück auf den normalen Modus."""
        self.log_queue.put(("progress_mode", (on, msg)))

    def _finish(self, kind, rc, extra=""):
        if rc == 0:
            self.log_queue.put(("log", f"=== {kind} erfolgreich abgeschlossen. ==="))
            self.log_queue.put(("status", f"{kind} erfolgreich abgeschlossen."))
            self.log_queue.put(("popup", (None,
                                          f"Das {kind} wurde erfolgreich abgeschlossen.", "success")))
        else:
            self.log_queue.put(("log", f"=== {kind} fehlgeschlagen (Code {rc}). {extra} ==="))
            self.log_queue.put(("status", f"{kind} fehlgeschlagen. {extra}"))
        self.log_queue.put(("busy", False))

    def _poll_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", payload + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "progress":
                    pct, msg = payload
                    if self.progress.cget("mode") != "determinate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    self.progress.set(max(0.0, min(1.0, pct / 100.0)))
                    self.status_var.set(msg)
                elif kind == "progress_mode":
                    on, msg = payload
                    if on:
                        self.progress.configure(mode="indeterminate")
                        self.progress.start()
                    else:
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    if msg:
                        self.status_var.set(msg)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "popup":
                    title, msg, popup_kind = payload
                    if popup_kind == "success":
                        CTkMsg.show_success(msg)
                    elif popup_kind == "info":
                        CTkMsg.showinfo(title, msg)
                    else:
                        CTkMsg.showerror(title, msg)
                elif kind == "busy":
                    self._set_busy(payload)
        except queue.Empty:
            pass
        self.after(200, self._poll_log_queue)

    def _set_busy(self, busy, kind=None):
        """Backup-/Restore-Buttons während eines laufenden Vorgangs anpassen.
        Der Button des GERADE LAUFENDEN Vorgangs (kind='backup'/'restore')
        bleibt bedienbar - beim Backup-Button sogar als aktiver 'Abbrechen'-
        Button, da Backups sauber abgebrochen werden können (siehe
        _cancel_backup). Der jeweils andere Button wird währenddessen
        deaktiviert. Schaltet außerdem das Icon zwischen Still-Bild (kein
        Vorgang aktiv/Vorgang beendet) und animierter Lotus-Sequenz (Vorgang
        läuft) um."""
        if busy:
            if kind == "backup":
                self.backup_start_btn.configure(state="normal", text="Backup abbrechen",
                                                 fg_color="#B71C1C", hover_color="#8f1616",
                                                 command=self._cancel_backup)
                self.restore_start_btn.configure(state="disabled", text="Bitte warten …",
                                                  fg_color="gray30")
            elif kind == "restore":
                self.restore_start_btn.configure(state="disabled", text="Restore läuft …",
                                                  fg_color="gray30")
                self.backup_start_btn.configure(state="disabled", text="Bitte warten …",
                                                 fg_color="gray30")
            else:
                self.backup_start_btn.configure(state="disabled", text="Bitte warten …", fg_color="gray30")
                self.restore_start_btn.configure(state="disabled", text="Bitte warten …", fg_color="gray30")
            self.set_icon_animated_gif()
        else:
            self.backup_start_btn.configure(state="normal", text="Backup starten",
                                             fg_color=self._default_btn_color,
                                             hover_color=self._default_btn_hover_color,
                                             command=self.start_backup)
            self.restore_start_btn.configure(state="normal", text="Restore starten",
                                              fg_color="#B71C1C")
            self.set_icon_still()

    def _cancel_backup(self):
        """Fordert den Abbruch eines laufenden Backups an. Die eigentliche
        Abbruch-/Aufräumlogik (Prozess killen, unfertige .part-Dateien
        löschen, OperationCancelled auslösen) läuft bereits vollständig in
        backup_device_to_file/gzip_compress_file/run_pkexec - hier wird nur
        das Event gesetzt, auf das diese Funktionen bereits reagieren."""
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        self.backup_start_btn.configure(state="disabled", text="Breche ab …")
        self._log("Abbruch angefordert - warte auf sauberes Beenden …")
        self._cancel_event.set()

    # ---------------- Geräte-Liste ----------------

    @staticmethod
    def _selected_index(combo, devices):
        """Mappt den angezeigten Text von src_menu/dst_menu (CTkComboBox)
        zurück auf den passenden Eintrag in der Geräteliste."""
        current = combo.get()
        for i, d in enumerate(devices):
            if d["display"] == current:
                return i
        return None

    def refresh_devices(self):
        self.devices = list_removable_devices()
        values = [d["display"] for d in self.devices] or ["Keine Geräte gefunden"]
        self.src_menu.configure(values=values)
        self.dst_menu.configure(values=values)
        self.src_menu.set(values[0])
        self.dst_menu.set(values[0])
        if not self.devices:
            self._log("Keine verifizierbaren Wechseldatenträger gefunden. "
                       "Hinweis: Geräte ohne Seriennummer/by-id-Pfad werden "
                       "aus Sicherheitsgründen nicht angezeigt.")


if __name__ == "__main__":
    install_askpass_helper()
    try:
        app = AuraPiApp()
        app.mainloop()
    finally:
        cleanup_askpass_helper()
