# Kursbuchungs-Bot fuer Aspria Hannover Maschsee

## Ziel

Ein privater Bot soll automatisch passende Kurse bei Aspria Hannover Maschsee buchen, sobald sie freigeschaltet werden. Zielkurse sind:

- `LES MILLS BODYPUMP`
- `Hyrox Starter`

Die Kurse werden ca. drei Kalendertage vor dem Kurstermin um etwa 21:00 Uhr freigeschaltet. Beispiel: Ein Kurs am Donnerstag, 07.05., ist ab Montag, 04.05. um ca. 21:00 buchbar.

## Nicht-Ziele fuer Version 1

- Keine automatische Stornierung.
- Keine automatische Umbuchung.
- Keine Kalenderintegration.
- Keine Mehrbenutzer-/Mehrprofil-Unterstuetzung.
- Kein Umgehen von Captcha, 2FA oder anderen Schutzmechanismen.
- Keine englische UI-Textlogik; die erste Version erwartet deutsche UI-Texte.

## Buchungsregeln

Die Regeln gelten fuer beide Kurse identisch.

Erlaubte Zeitfenster:

- Montag: nicht buchen
- Dienstag: Startzeit bis einschliesslich 11:00
- Mittwoch: nicht buchen
- Donnerstag: Startzeit ab einschliesslich 18:00
- Freitag: Startzeit ab einschliesslich 18:00
- Samstag: Startzeit zwischen einschliesslich 09:00 und 20:00
- Sonntag: Startzeit zwischen einschliesslich 09:00 und 20:00

Kursnamen werden exakt gematcht, nach Normalisierung von Gross-/Kleinschreibung und Leerzeichen. Kein fuzzy Matching.

Pro Kurstyp ist maximal eine Buchung pro Tag erlaubt. Es duerfen aber beide Kurse am selben Tag gebucht werden, wenn beide in die erlaubten Zeitfenster fallen und sich nicht ueberschneiden.

Bei mehreren erlaubten Zeiten desselben Kurstyps am selben Tag wird die frueheste erlaubte Startzeit gewaehlt.

Bestehende manuelle Buchungen werden respektiert:

- Keine Buchung bei zeitlicher Ueberschneidung.
- Keine zweite Buchung desselben Kurstyps am selben Tag.
- Keine Veraenderung bestehender Buchungen.

Ueberschneidungen werden mit Kursdauer berechnet. Wenn die Dauer auf der Seite sichtbar ist, wird sie verwendet. Falls nicht, gilt:

- Standarddauer: 60 Minuten
- Puffer: 15 Minuten

## Buchungs- und Wartelistenverhalten

Entscheidungsreihenfolge:

1. Wenn der Kurs bereits gebucht ist: nichts tun.
2. Wenn der Kurs bereits auf Warteliste steht: nichts tun; Warteliste ist Endzustand.
3. Wenn ein Platz frei ist: sofort buchen.
4. Wenn der Kurs voll ist und Warteliste moeglich ist: automatisch der Warteliste beitreten.
5. Wenn voll und keine Warteliste moeglich ist: per E-Mail melden.
6. Wenn der Status unklar ist: Screenshot/HTML speichern und Fehler-E-Mail senden.

Nach erfolgreicher Wartelisten-Anmeldung wird der Kurs nicht weiter beobachtet und spaeter nicht automatisch in eine Buchung umgewandelt.

## Jobs und Zeitplanung

Der Bot laeuft auf einem kleinen Linux-Server per systemd Timer.

Release-Job:

- Startet taeglich um 20:55.
- Prueft Login/Session und navigiert zur Kursseite.
- Polling von 20:58 bis 21:10 alle 5-10 Sekunden.
- Danach bei verspaeteter Freischaltung optional alle 5 Minuten bis 22:00.
- Zieltag ist genau heute + 3 Kalendertage.

Stundenjob:

- Laeuft stuendlich.
- Prueft heute bis heute + 3 Tage inklusive.
- Bucht passende freie Kurse sofort.
- Tritt passenden Wartelisten sofort bei.

Manueller Scan:

```text
aspria-booker scan --from 2026-05-04 --to 2026-05-07 --dry-run
```

Der manuelle Scan ist standardmaessig fuer Kontrolle und Debugging gedacht. Im Dry-Run sendet er keine E-Mail, ausser `--notify` wird explizit gesetzt.

## Technische Basis

Stack:

- Python 3.12+
- Playwright mit Chromium
- `uv` fuer Projekt- und Dependency-Management
- YAML-Konfiguration
- `.env` fuer Secrets
- SQLite fuer lokale Historie
- systemd Timer fuer Betrieb

Vorgesehene CLI-Kommandos:

```text
aspria-booker setup-login
aspria-booker release
aspria-booker hourly
aspria-booker scan
aspria-booker test-email
```

## Login und Session

Initiales Login:

- Lokal mit sichtbarem Browser ausfuehren:

```text
uv run aspria-booker setup-login --headed
```

- Der Nutzer loggt sich interaktiv ein.
- Playwright speichert den Storage-State, z. B. `storage/aspria-state.json`.
- Die Storage-State-Datei wird auf den Server uebertragen.

Automatisches Neu-Login:

- Wenn die Session ungueltig ist, versucht der Bot headless Login mit Benutzername und Passwort aus `.env`.
- Bei erfolgreichem Login wird der Storage-State aktualisiert.
- Bei Captcha, 2FA oder ungewoehnlicher Seite stoppt der Bot, speichert Artefakte und sendet eine E-Mail.

Die echte Buchungs-URL soll nach Discovery gespeichert werden. Spaetere Jobs starten direkt auf dieser URL. Falls sie nicht funktioniert, gibt es einen Fallback ueber:

```text
https://www.aspria.com/de/hannover-maschsee
```

und den Link/Button `Kurs Buchen`.

## Konfiguration

Beispielstruktur:

```yaml
enabled: true
dry_run: true

club: hannover-maschsee
booking_window_days: 3
release_time: "21:00"
release_job:
  start: "20:55"
  poll_start: "20:58"
  poll_stop: "21:10"
  slow_poll_stop: "22:00"
  interval_seconds: 10
  slow_interval_minutes: 5

hourly_job:
  lookahead_days: 3

matching:
  language: de
  exact_course_names: true

default_duration_minutes: 60
buffer_minutes: 15

retention:
  error_artifacts_days: 14
  history_days: 90

courses:
  - name: "LES MILLS BODYPUMP"
    max_per_day: 1
    waitlist: true
  - name: "Hyrox Starter"
    max_per_day: 1
    waitlist: true

time_windows:
  tue:
    - end: "11:00"
  thu:
    - start: "18:00"
  fri:
    - start: "18:00"
  sat:
    - start: "09:00"
      end: "20:00"
  sun:
    - start: "09:00"
      end: "20:00"
```

Config-Validierung soll streng sein und bei Fehlern abbrechen, z. B. bei:

- unbekannten Wochentagen
- ungueltigen Zeitformaten
- leeren Kursnamen
- negativen Dauern oder Puffern
- fehlenden SMTP-Werten bei aktiver Benachrichtigung
- `dry_run: false` ohne Zugangsdaten

`enabled: false` dient als Kill Switch. `dry_run: true` erlaubt Scans und Entscheidungen, aber keine echten Buchungs- oder Wartelistenklicks. Ein CLI-Flag `--dry-run` darf immer in die sichere Richtung ueberschreiben.

## Secrets

Secrets liegen in `.env`, nicht im Code:

```text
ASPRIA_EMAIL=
ASPRIA_PASSWORD=
SMTP_HOST=
SMTP_PORT=
SMTP_USER=
SMTP_PASSWORD=
NOTIFY_TO=
```

`.env` und `storage/` muessen in `.gitignore`. Auf dem Server sollen die Dateien nur fuer den Bot-User lesbar sein.

## Benachrichtigung

Benachrichtigung erfolgt per E-Mail.

E-Mail senden bei:

- Login erfolgreich nach neuer Anmeldung
- Kurs erfolgreich gebucht
- Warteliste erfolgreich beigetreten
- passender Kurs nicht gefunden
- Kurs gefunden, aber Buchung fehlgeschlagen
- Login braucht manuellen Eingriff
- unerwartete Seitendarstellung oder technischer Fehler

E-Mails enthalten eine `run_id`. Erfolgs- und Wartelistenmails werden pro Kursaktion nur einmal gesendet. Derselbe bekannte Fehler wird hoechstens einmal pro Tag gemeldet.

Fehler-HTMLs werden nicht per E-Mail angehaengt. Die Mail enthaelt nur Beschreibung und lokalen Artefaktpfad auf dem Server.

## Historie und Artefakte

SQLite-Historie:

- `runs`
- `course_observations`
- `actions`
- `notifications`

Aufbewahrung: 90 Tage.

Fehlerartefakte:

- Screenshots und HTML nur bei Fehlern oder unklarem Status.
- Pfad z. B. `artifacts/errors/`.
- Aufbewahrung: 14 Tage.
- Keine Passwoerter, Cookies oder Auth-Header speichern.

Discovery-Artefakte:

- Screenshots
- HTML
- relevante Network-Request-URLs und Statuscodes
- keine Passwoerter
- keine Cookies/Auth-Header
- Request/Response-Bodies nur wenn noetig und moeglichst ohne personenbezogene Daten

## Discovery-Phase

Vor echter Buchungslogik wird ein Discovery-Script gebaut.

Ziele:

- Login-Flow verstehen.
- Finale Kursplan-/Buchungs-URL ermitteln.
- Kursliste finden.
- Datumsauswahl verstehen.
- Deutsche Button-/Status-Texte erfassen.
- Status fuer frei, voll, Warteliste, gebucht erkennen.
- Bestaetigungsdialoge und Erfolgsmeldungen erkennen.
- Screenshots und HTML fuer die Implementierung sammeln.

Die Implementierung soll deutsche UI-Texte als primaere Wahrheit verwenden. Eindeutige technische Statusattribute aus DOM/API duerfen nach Discovery zusaetzlich verwendet werden.

## Sicherheit und Regelkonformitaet

Der Bot soll stoppen und E-Mail senden bei:

- Captcha
- 2FA
- Hinweis auf verdaechtige Aktivitaet
- unklarem Bot-Verbot oder explizitem Automationsverbot
- unerwarteten Login- oder Buchungsseiten

Kein paralleles Polling, keine Massenzugriffe und kein Umgehen technischer Schutzmechanismen.

## Tests

Version 1 soll fokussierte Unit-Tests enthalten fuer:

- YAML-Config-Validierung
- exaktes Kursname-Matching
- Zeitfenster-Regeln
- deutsche Statusklassifikation
- Ueberschneidungslogik mit Dauer und Puffer
- Entscheidungslogik fuer buchen, Warteliste oder nichts tun
- E-Mail-Rendering ohne SMTP-Versand

Nicht als Unit-Test erforderlich:

- echte Aspria-Seite
- echtes Login
- echte Buchung

## Runbook

Eine README soll enthalten:

- Installation mit `uv`
- Playwright Chromium installieren
- `.env` und `config.yaml` anlegen
- `setup-login`
- `test-email`
- `scan --dry-run`
- systemd Timer installieren
- typische Fehler und Gegenmassnahmen
- Pfade fuer Artefakte und SQLite
- Nutzung von `enabled` und `dry_run`

