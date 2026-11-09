# DupeFinder

Findet identische Dateien (SHA-256) und optisch gleiche Bilder (pHash) unter `/data`.

## Start
    docker compose up -d --build      # UI: http://<host>:8090

Alles, was geprüft werden soll, in `docker-compose.yml` unter `/data/...` einhängen.
`/config` enthält SQLite (`dupefinder.db`) und Thumbnail-Cache.

## Ablauf
1. Links im Baum Laufwerke (oberste Ebene unter `/data`) oder beliebige Unterordner anhaken.
   - **ein** Ordner: Duplikate nur innerhalb dieses Ordners (inkl. Unterordner)
   - **mehrere** Ordner: Suche über alle zusammen, also innerhalb jedes Ordners und zwischen ihnen
   - Schalter *Nur Treffer zwischen den Ordnern*: Duplikate innerhalb eines einzelnen Ordners werden ignoriert
   - Ein Unterordner eines bereits gewählten Ordners ist automatisch enthalten (grau). Wählst du einen Elternordner, gehen gewählte Unterordner darin auf.
   - Die Statuszeile unten zeigt immer, was mit der aktuellen Auswahl gesucht wird.
2. Modus: *Identische Dateien* oder *Ähnliche Bilder* (Toleranz = max. Hamming-Distanz, 0–16, Standard 6).
3. **Suche starten**. Ein zweiter Lauf ist schnell, weil nur neue oder geänderte Dateien (Größe/mtime) neu gehasht werden.
4. Treffer tragen die farbige Nummer des Ordners. Klick aufs Thumbnail zeigt die Gruppe nebeneinander (← → / Esc).
5. *Auto-Auswahl* behält je Gruppe die Datei aus dem zuerst gewählten Ordner mit der höchsten Auflösung.
   **In Papierkorb** verschiebt nach `<Laufwerk>/.dupefinder-trash/<Pfad>`. Es wird nichts gelöscht, und bei `:ro`-Mounts geht es nicht.

## Verfahren
- Exakt: Größe vorfiltern → SHA-256 nur bei Größenkollision.
- Bilder: pHash (DCT, 64 Bit) nach EXIF-Drehung; Gruppierung per BK-Tree + Union-Find.
  Erkennt: Re-Encode, andere Auflösung/Qualität, PNG↔JPG, leichte Filter. Nicht: 90°-Drehung, Spiegelung, starker Zuschnitt.

## ENV
| Var | Default |
|---|---|
| WORKERS | 4 |
| EXCLUDE | `@eaDir,#recycle,#snapshot,.Trash-1000,.dupefinder-trash` |
| DATA_ROOT / DB_PATH / CACHE_DIR | `/data`, `/config/dupefinder.db`, `/config/thumbs` |

Formate: Bilder jpg, png, gif, bmp, webp, tif, heic/heif (HEIC wird für den Browser zu JPEG). Videos mp4, m4v, mov, webm (im Browser abspielbar) sowie mkv, avi, 3gp, mts, wmv, mpg (Vorschaubild + Download). Vorschaubilder für Videos per ffmpeg, Dauer und Auflösung per ffprobe.

## Ersparnis & Protokoll
Jede in den Papierkorb verschobene Datei wird in `trash_log` der SQLite protokolliert (Zeit, Pfad, Größe, SHA-256, Papierkorb-Pfad).
Die Summe „Bisher eingespart“ oben rechts kommt daraus – `config/dupefinder.db` daher nicht löschen.

    sqlite3 config/dupefinder.db "select datetime(ts,'unixepoch','localtime'),size,path from trash_log order by ts desc limit 20"

Thumbnails liegen zweistufig unter `config/thumbs/ab/cd/abcd….jpg` (kann jederzeit gelöscht werden).
Alte einstufige Thumbs (`thumbs/ab/*.jpg`) werden nicht mehr benutzt und können weg.

## Großansicht
Zurück (oben links), Esc oder Backspace schließen · ← → Gruppe wechseln · Tasten 1–5 markieren die jeweilige Datei. Es werden max. 5 Dateien einer Gruppe nebeneinander gezeigt; bei mehr blättert ‹ › oben (oder Shift+← →). Nach „In Papierkorb“ rücken die übrigen Dateien nach, die Gruppe bleibt offen.
Blauer Button **Infos** je Datei: Maus darüber (oder antippen) zeigt alle Metadaten schwebend. „Details ein-/ausblenden“ oben schaltet die ausführlichen Texte unter den Bildern (Standard: an bei bis zu 3 Dateien).
Im Papierkorb öffnet ein Klick aufs Vorschaubild die Großansicht im selben Fenster (Zurück, ← →, Infos, Wiederherstellen, Endgültig löschen).

## Metadaten
Für alle Dateien in den Ergebnisgruppen werden einmalig gelesen und in der SQLite (`files.meta`) gecacht:
- Bilder: EXIF-Aufnahmedatum, Kamera, Objektiv, Belichtung, GPS, Software
- Videos (ffprobe): creation_time / Apple-creationdate, Kamera, GPS (ISO 6709), Codec, fps, Bitrate
- Datei: Erstellzeit per `statx()` (nur wenn Kernel ≥ 4.11 und Dateisystem sie liefern, sonst leer), mtime, ctime

Zeile „Metadaten“ in der Kachel: Aufnahmedatum → sonst Erstelldatum → sonst Änderungsdatum. Maus darüber = Details, Großansicht = ausführlich mit Kartenlink.

## Papierkorb
- Button **Papierkorb** oben (Zähler = Dateien im Papierkorb), im Baum ein Badge je Laufwerk und beim Aufklappen ein Knoten „Papierkorb“.
- Ansicht gruppiert nach Herkunftsordner; Wiederherstellen (einzeln, je Ordner, alle) an den Originalort. Ist der Name dort wieder belegt, wird als `Name (wiederhergestellt).ext` abgelegt.
- **Endgültig löschen** mit Rückfrage. Nach jedem Verschieben erscheint 10 s lang „Rückgängig“.
- Großansicht: „In Papierkorb“ pro Datei (sofort, rückgängig machbar), oben „Markierte in Papierkorb“, Taste `Entf` = markierte Dateien der aktuellen Gruppe.
- Quelle der Wahrheit sind die `.dupefinder-trash`-Ordner; außerhalb gelöschte Dateien werden im Log als `gone` markiert (zählen weiter zur Ersparnis, wiederhergestellte nicht).

## Dateitypen
Eigene Icons für ~20 Kategorien (Office/LibreOffice, PDF, Text, Code, Skripte, Programme/Binärdateien, Archive, Disk-Images, Audio, Schriften, Datenbanken, E-Mail, E-Books, Design, RAW-Fotos, Kalender/Kontakte, Schlüssel/Zertifikate, Verknüpfungen, 3D/CAD).
Großansicht: PDF im Browser-Viewer, Textdateien als Klartext (max. 512 KB, ohne HTML/JS-Ausführung), Audio mit Player, sonst Icon + Download.
PDF-Vorschaubilder der ersten Seite per `pdftoppm` (poppler-utils im Image).

## Infos vergleichen
In der Großansicht „Infos“ anklicken → Panel bleibt offen; mehrere gleichzeitig möglich, „Alle Infos“ oben öffnet alle sichtbaren.
Werte, die von der ersten Datei abweichen, sind gelb markiert. Maus über „Infos“ zeigt weiterhin die schnelle Vorschau.

## Index-Abgleich („auch schon gefunden in“)
Jeder Scan merkt sich Pfad, Größe und mtime aller Dateien. Beim nächsten Scan eines anderen Ordners werden Größen gegen den **ganzen Index** verglichen:
nur bei gleicher Größe wird gehasht – erst ein Schnell-Hash (erste + letzte 64 KB), der volle SHA-256 nur bei gleichem Schnell-Hash.
Treffer außerhalb der Auswahl werden vorher auf Existenz geprüft und gestrichelt mit „↗ auch schon gefunden in“ angezeigt.
- Schalter „Mit Index abgleichen“ (Standard an)
- Schalter „Kopien außerhalb behalten“ (Standard an): die Kopie außerhalb gilt als Original, Auto-Auswahl markiert alle Kopien in der Auswahl. Dateien außerhalb werden nie automatisch markiert.
- Bei „Ähnliche Bilder“ werden nur Bilder außerhalb berücksichtigt, die schon einen pHash haben.

## Aufräumen
- Scan entfernt Index-Einträge unterhalb der gescannten Ordner, deren Datei nicht mehr existiert.
- Verschieben/Löschen entfernt die zugehörigen Vorschaubilder.
- Endgültig gelöschte Dateien: im Protokoll bleiben Zeit, Größe und SHA-256 (für „Bisher eingespart“ und „Kopie gelöscht“), Pfade werden entfernt. Mit `KEEP_DELETE_LOG=1` bleiben auch die Pfade.
- Unten rechts „aufräumen“: prüft den ganzen Index, löscht verwaiste Vorschaubilder, anonymisiert alte Protokolleinträge, `VACUUM`.

## Index-Kennzeichnung im Baum
Kleines „I“ hinter dem Ordnernamen (Tabelle `scans` merkt sich jeden gescannten Ordner mit Zeitpunkt):
- grün: komplett im Index, seit dem Scan keine Dateien in diesem Ordner hinzugefügt/gelöscht (Ordner-mtime)
- orange: im Index, aber Ordnerinhalt hat sich seitdem geändert → neu scannen
- gestrichelt: nur einzelne Unterordner wurden gescannt
Hinweis: die Prüfung nutzt die mtime des jeweiligen Ordners – geänderte Dateiinhalte oder Änderungen tiefer in Unterordnern erkennt erst der nächste Scan (Unterordner zeigen beim Aufklappen ihren eigenen Status).

## Ordneransicht
- Klick auf eine Datei öffnet die Großansicht (Bild, Video, Audio, PDF, Text, sonst Icon); ← → blättert im Ordner, Download-Symbol am Zeilenende lädt direkt herunter.
- Buttons: Download · In Papierkorb (mit Rückgängig) · Infos (anheftbar) · „Hash berechnen“, falls noch kein SHA-256 bekannt.
- „Index & Kopien“: Index-Status, Schnell-Hash, alle Dateien mit gleichem SHA-256 (✓ vorhanden / ⚠ geändert / ✗ fehlt, Klick öffnet den Ordner) und bereits entfernte Kopien laut Protokoll.
- Große Ordner werden in Blöcken zu 300 Einträgen beim Scrollen nachgeladen.

## Behalten-Regel
Links unten: Original-Ordner per ☆ im Baum festlegen (★1, ★2 … Reihenfolge mit ↑/↓). Pro Gruppe gilt: Kopie im ★1-Ordner → ★2 … → Kopie außerhalb der Auswahl (Schalter) → zuerst gewählter Ordner.
Mehrere Kopien im selben Ordner: höchste Auflösung / älteste Datei / kürzester Pfad. Wirkt sofort auf „behalten“ und Auto-Auswahl, ohne Neuscan.

## Ergebnisliste
Sortierung (Einsparung, Dateigröße, Anzahl, Datum, Pfad) und Filter (Bilder, Videos, Dokumente, nur Index-Treffer); Gruppen werden in Blöcken zu 40 nachgeladen. ← → in der Großansicht folgt der Listenreihenfolge.

## Baum aktuell halten
Automatisch jede Minute (nur bei sichtbarem Tab) und beim Zurückwechseln in den Tab; „↻ neu laden“ manuell.
Gelöschte Ordner verschwinden aus Baum, Auswahl und Behalten-Regel (Hinweis); ein gelöschter offener Ordner springt zum nächsten vorhandenen Elternordner.
