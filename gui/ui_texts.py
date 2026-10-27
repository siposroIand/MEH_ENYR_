# ui_texts.py
# Központi, formális magyar felhasználói szövegek az alkalmazáshoz.
APP_TITLE = "MÉH Zrt. — Engedély Nyilvántartó Rendszer"
APP_NAME_SHORT = "MEH - Engedély"

# Általános hibák / információk
MSG_NETWORK_ROOT_UNAVAILABLE_TITLE = "Hálózati gyökér elérésének hibája"
MSG_NETWORK_ROOT_UNAVAILABLE_BODY = (
    "A megadott hálózati gyökér nem elérhető vagy nem írható. "
    "Ellenőrizze a hálózati kapcsolatot és a jogosultságokat, majd indítsa újra az alkalmazást."
)
MSG_DB_INIT_FAILED_TITLE = "Adatbázis inicializálási hiba"
MSG_DB_INIT_FAILED_BODY = (
    "Az alkalmazás adatbázisának inicializálása nem sikerült. "
    "Ellenőrizze a lemezterületet és a jogosultságokat, majd próbálja meg újraindítani az alkalmazást."
)

# Gombok / dialógusok
BTN_SAVE = "Mentés"
BTN_CANCEL = "Mégse"
BTN_OK = "Rendben"
BTN_CLOSE = "Bezárás"
BTN_YES = "Igen"
BTN_NO = "Nem"
BTN_BACKUP = "Biztonsági mentés készítése"
BTN_RESTORE = "Visszaállítás a kiválasztott mentésből"
BTN_OPEN_FOLDER = "Megnyitás mappában"

# Dialógusok / címek
DIALOG_ENGEDELY_TITLE = "Engedély adatlap"
DIALOG_TELEPHELY_TITLE = "Telephely adatok"
DIALOG_BACKUP_TITLE = "Biztonsági mentések"
DIALOG_EXPIRY_TITLE = "Lejáró engedélyek"
DIALOG_REVIEWS_TITLE = "Felülvizsgálatok"

# Figyelmeztetések, megerősítések
WARN_MISSING_TELEPHELY = "Válasszon telephelyet a listából."
WARN_MISSING_NUMBER = "A határozatszám megadása kötelező."
CONFIRM_DELETE = "Biztosan törli a kiválasztott elemet?"
CONFIRM_DELETE_TELEPHELY = "Biztosan törli a kiválasztott telephelyet?"
INFO_NO_ATTACHMENTS = "Ehhez a tételhez nincsenek csatolmányok."
ERR_OPEN_ATTACHMENT = "A csatolmány megnyitása sikertelen."
ERR_COPY_ATTACHMENT = "A fájl másolása sikertelen: {fname}"
ERR_ATTACHMENT_TOO_LARGE = "A csatolmány túl nagy és nem került átmásolásra: {fname} ({size} MB, korlát: {limit} MB)"
ERR_ATTACHMENT_FORBIDDEN = "A csatolmány típusa nem engedélyezett és nem került átmásolásra: {fname}"

# Backup / restore üzenetek
INFO_BACKUP_CREATED = "Biztonsági mentés elkészült:\n{path}"
INFO_BACKUP_NONE = "Nincs menteni való adatbázis fájl."
INFO_RESTORE_SUCCESS = "A mentés visszaállítása sikeres. Az alkalmazás újraindul a változások érvényesítéséhez."
ERR_RESTORE_FAILED = "Visszaállítás sikertelen: {detail}"

# Export / file
INFO_EXPORT_SUCCESS = "Az exportálás sikeresen elkészült."
ERR_EXPORT_FAILED = "Exportálás sikertelen: {detail}"

# Napló / audit
LOG_BACKUP_MANUAL = "backup_manual"
LOG_ENGEDELY_INSERT = "engedely_insert"
LOG_ENGEDELY_UPDATE = "engedely_update"
LOG_ENGEDELY_DELETE = "engedely_delete"
LOG_ENGEDELY_ARCHIVE = "engedely_archive_toggle"