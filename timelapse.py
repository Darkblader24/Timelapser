import os
import glob
import pathlib
import re
import shutil
import subprocess

# Überprüfung der Python-Abhängigkeiten
try:
    import click
except ModuleNotFoundError:
    raise ModuleNotFoundError(
        "Das Modul 'click' ist nicht installiert. Bitte installieren Sie es oder kontaktieren Sie Ihren Systemadministrator."
    )

# Überprüfung der System-Abhängigkeiten (FFmpeg)
if not shutil.which("ffmpeg"):
    raise RuntimeError(
        "Das Systemprogramm 'ffmpeg' wurde nicht gefunden. Bitte installieren Sie FFmpeg und fügen Sie "
        "es zu Ihrer PATH-Umgebungsvariable hinzu, oder kontaktieren Sie Ihren Systemadministrator."
    )


def detect_encoder(encoder):
    """
    Prüft verfügbare FFmpeg-Encoder und wählt einen kompatiblen GPU-Encoder aus, falls vorhanden.
    Falls kein Grafikkarten-Encoder gefunden wird, wird der CPU-Standard 'libx264' zurückgegeben.
    """
    if encoder != "auto":
        return encoder

    try:
        result = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True, check=True)
        encoders = result.stdout

        # Prioritätenliste für Hardware-Beschleunigung (NVIDIA, Apple, AMD, Intel)
        if "h264_nvenc" in encoders:
            return "h264_nvenc"
        if "h264_videotoolbox" in encoders:
            return "h264_videotoolbox"
        if "h264_amf" in encoders:
            return "h264_amf"
        if "h264_qsv" in encoders:
            return "h264_qsv"
    except Exception:
        pass
    return "libx264"


def extract_number(filename):
    """Extrahiert die erste zusammenhängende Zahlenfolge aus einem Dateinamen."""
    match = re.search(r"\d+", os.path.basename(filename))
    return int(match.group()) if match else None


@click.command()
@click.option(
    "--input-dir", "-i",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, readable=True),
    prompt="Pfad zum Verzeichnis mit den Quellbildern",
    default=str(pathlib.Path.cwd() / "images"),
)
@click.option(
    "--output-path", "-o",
    type=click.Path(writable=True),
    prompt="Pfad der Ausgabedatei",
    default=str(pathlib.Path.cwd() / "videos" / "timelapse.mp4"),
)
@click.option(
    "--fps", "-f",
    type=int,
    prompt="Bilder pro Sekunde (FPS) für das resultierende Video",
    default=30,
)
@click.option(
    "--pattern", "-p",
    type=str,
    prompt="Glob-Suchmuster für Bilder (z.B. *.jpg oder DSC*.JPG). Achtung: Groß-/Kleinschreibung beachten",
    default="*.JPG",
)
@click.option(
    "--start-num", "-s",
    type=int,
    prompt="Start-Sequenznummer im Dateinamen (0 für von Anfang an)",
    default=0,
)
@click.option(
    "--end-num", "-e",
    type=int,
    prompt="End-Sequenznummer im Dateinamen (0 für bis zum Ende)",
    default=0,
)
@click.option(
    "--resolution", "-r",
    type=str,
    prompt="Zielauflösung (BreitexHöhe, wird zentriert zugeschnitten)",
    default="3840x2160",
)
@click.option(
    "--quality", "-q",
    type=click.Choice(["high", "medium", "low"], case_sensitive=False),
    prompt="Qualitätsstufe des Ausgabevideos",
    default="medium",
)
@click.option(
    "--speed", "-sp",
    type=click.Choice(["slow", "medium", "fast"], case_sensitive=False),
    prompt="Kodierungsgeschwindigkeit (Trade-off: Datei-Größe vs. Zeit)",
    default="medium",
)
@click.option(
    "--encoder",
    type=click.Choice(["auto", "libx264", "h264_nvenc", "h264_videotoolbox", "h264_amf", "h264_qsv"], case_sensitive=False),
    prompt="Video-Encoder (auto erkennt die beste Option basierend auf der Hardware)",
    default="auto",
)
def create_timelapse(input_dir, output_path, fps, pattern, start_num, end_num, resolution, quality, speed, encoder):
    """
    Ein Skript zur Erstellung eines Zeitraffer-Videos aus einer Bildsequenz mittels FFmpeg.
    Falls Argumente im CLI weggelassen werden, fragt das Programm diese interaktiv ab.
    """
    click.echo("\n--- Start der Zeitraffer-Konfiguration ---")
    click.echo(f"Eingabeverzeichnis:   {input_dir}")
    click.echo(f"Ausgabevideo:         {output_path}")
    click.echo(f"Framerate (FPS):      {fps}")
    click.echo(f"Suchmuster:           {pattern}")
    click.echo(f"Start-Nummer:         {start_num if start_num > 0 else 'Anfang'}")
    click.echo(f"End-Nummer:           {end_num if end_num > 0 else 'Ende'}")
    click.echo(f"Zielauflösung:        {resolution}")
    click.echo(f"Qualität:             {quality}")
    click.echo(f"Geschwindigkeit:      {speed}\n")
    click.echo(f"Encoder:              {encoder}\n")

    # Alle Dateien sammeln und sortieren
    search_pattern = os.path.join(input_dir, pattern)
    all_images = sorted(glob.glob(search_pattern))

    if not all_images:
        click.echo(f"Fehler: Keine Bilder mit dem Muster \"{pattern}\" in \"{input_dir}\Und darunter sol" gefunden.", err=True)
        return

    # Bilder basierend auf den Sequenznummern filtern (z.B. DSC00800.JPG bis DSC01000.JPG)
    images = []
    for img in all_images:
        num = extract_number(img)
        if num is not None:
            if start_num > 0 and num < start_num:
                continue
            if end_num > 0 and num > end_num:
                continue
        images.append(img)

    if not images:
        click.echo("Fehler: Keine Bilder entsprechen den angegebenen Start- und End-Filterkriterien.", err=True)
        return

    click.echo(f"{len(images)} von {len(all_images)} Bildern entsprechen den Kriterien. Video wird erstellt...")

    # Auflösung für den Zuschnitt-Filter parsen
    try:
        width, height = resolution.lower().split("x")
        width, height = int(width), int(height)
    except ValueError:
        click.echo("Fehler: Ungültiges Auflösungsformat. Bitte nutzen Sie 'BreitexHöhe' (z.B. 3840x2160).", err=True)
        return

    # Zielverzeichnis erstellen falls nicht vorhanden
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # GPU Hardware-Beschleunigung erkennen
    encoder = detect_encoder(encoder)
    click.echo(f"Ausgewählter Video-Encoder: {encoder}")

    # FFmpeg-Basisbefehl aufbauen
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",  # Vorhandene Ausgabedatei ohne Nachfrage überschreiben
        "-f", "image2pipe",  # Eingabeformat ist ein fortlaufender Stream aus Bildern
        "-r", str(fps),  # Framerate der Eingabe festlegen
        "-i", "-",  # Daten kommen von stdin (Pipe)
        "-loglevel", "error", # Nur Fehler ausgeben, um deadlocks durch eine volle stdout pipe zu vermeiden
    ]

    # Encoder-spezifische Parameter für Qualität und Geschwindigkeit setzen
    if encoder == "libx264":
        ffmpeg_cmd += ["-c:v", "libx264"]
        ffmpeg_cmd += ["-preset", speed]
        crf_map = {"high": "18", "medium": "23", "low": "28"}
        ffmpeg_cmd += ["-crf", crf_map[quality]]
    elif encoder == "h264_nvenc":
        ffmpeg_cmd += ["-c:v", "h264_nvenc"]
        ffmpeg_cmd += ["-preset", speed]
        cq_map = {"high": "20", "medium": "26", "low": "32"}
        ffmpeg_cmd += ["-rc", "vbr", "-cq", cq_map[quality]]
    elif encoder == "h264_videotoolbox":
        ffmpeg_cmd += ["-c:v", "h264_videotoolbox"]
        q_map = {"high": "90", "medium": "75", "low": "50"}
        ffmpeg_cmd += ["-q:v", q_map[quality]]
    else:
        # Fallback für andere GPU-Encoder (AMF oder QSV)
        ffmpeg_cmd += ["-c:v", encoder]
        ffmpeg_cmd += ["-preset", speed]

    # Video-Filterkette: Erst proportional so skalieren, dass das Bild die Box vollständig ausfüllt (decken),
    # danach zentrierter Zuschnitt (crop) + Pixelformat yuv420p.
    scale_filter = f"scale='max({width},iw*{height}/ih)':'max({height},ih*{width}/iw)'"
    crop_filter = f"crop=w={width}:h={height}:x=(iw-ow)/2:y=(ih-oh)/2"

    ffmpeg_cmd += ["-vf", f"{scale_filter},{crop_filter},format=yuv420p"]
    ffmpeg_cmd += [output_path]

    print("Executing: " + " ".join(ffmpeg_cmd))

    # FFmpeg-Prozess starten
    process = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )

    # Bilder einlesen und in die FFmpeg-Pipe schreiben
    with click.progressbar(images, label="Video wird mittels FFmpeg kodiert") as bar:
        for img_path in bar:
            try:
                with open(img_path, "rb") as f:
                    process.stdin.write(f.read())
            except (IOError, OSError):
                # Beschädigte oder unlesbare Einzelbilder überspringen
                continue
            except BrokenPipeError:
                # Falls FFmpeg vorzeitig abstürzt, stoppen wir das Schreiben sofort
                break

    # Pipe schließen und auf das Ende von FFmpeg warten
    stderr_output = process.communicate()[1]

    if process.code != 0 if hasattr(process, 'code') else process.returncode != 0:
        click.echo(f"\nFFmpeg-Fehler (Code {process.returncode}):", err=True)
        click.echo(stderr_output.decode("utf-8", errors="ignore"), err=True)
        return

    click.echo("\nErfolg: Zeitraffer wurde erfolgreich erstellt!")


if __name__ == "__main__":
    create_timelapse()
