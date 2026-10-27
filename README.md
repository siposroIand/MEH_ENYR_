# MEH – Engedélynyilvántartó

Magyar nyelvű Python/PySide6 asztali alkalmazás. A telephelyek, engedélykategóriák,
azonosítók, archívum, határidők, keresés és PDF/Excel/egyéni riportok megmaradnak.
Az **engedélyügy felelőse** nem azonos a program használati jogosultságával.

## Felépítés és egyszerűsítések

- `core/db.py`: tárolási életciklus, tranzakciók, migrációk, revíziók és helyreállítás.
- `core/auth.py`: hitelesítés, visszavonható munkamenetek és fiókszabályok.
- `core/services.py`: a GUI által hívható, jogosultságellenőrzött ügy- és telephelyműveletek.
- `utils/reviews.py`: közös felülvizsgálati értelmezés a képernyők és exportok számára.
- `core/attachments.py`: közös fájlpolitika és visszavonható csatolmánytervezet.
- `gui/`: a meglévő magyar táblázatos munkafolyamat; nincs közvetlen SQL-írás.

Az új ügy a kiválasztott telephelyet/kategóriát ajánlja fel, a saját ügyek egy
szűrővel elérhetők, és több ügy felelőse egyszerre kijelölhető. A hibás mentés
nem üríti ki az űrlapot. A törlés és a tartós csatolmány eltávolítása továbbra is
megerősítést kér; nem automatizálunk jogszabályi vagy hatósági döntést.

## Telepítés és indítás

Python 3.10–3.13, működő grafikus környezet és a Qt rendszerkönyvtárai szükségesek.
A rögzített függőségeket Python 3.12/Linux környezetben ellenőriztük;
a Windows és más Python-verziók külön átvételi próbát igényelnek.
Javasolt külön virtuális környezet:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

Az alapértelmezett adatgyökér a korábbi
`M:\home\IT apps\MEH_Programok\MEH_Engedelynyilvantarto\#adatbazis`.
Másik, **már létező, abszolút** könyvtár beállítása:

```powershell
$env:MEH_DATA_ROOT = "C:\MEH-adatok"
python main.py
```

Linuxon például `MEH_DATA_ROOT=/srv/meh python main.py`. A `db/engedelyek.db`,
`attachment/` és `backup/` alkönyvtárak elrendezése változatlan. Az elérhetetlen
adatgyökér hibát okoz: nincs automatikus, üres helyettesítő adatbázis a munkakönyvtárban.
Indítás előtt ellenőrizze, hogy valóban a korábbi adatgyökeret adta meg.

## Fiókok és felelősség

Üres felhasználói táblán az első indítás rendszergazda létrehozását kéri.
Nincs alapértelmezett jelszó és nyilvános önregisztráció. Több egyidejű első
indításból csak egy hozhat létre első rendszergazdát.

| Művelet | Rendszergazda | Ügyintéző | Megtekintő |
|---|---|---|---|
| Közös nyilvántartás, keresés, export | igen | igen | igen |
| Új engedélyügy | igen | igen, saját felelősséggel | nem |
| Engedély módosítása, archiválása, törlése, csatolmány módosítása | bármelyik | csak saját ügy | nem |
| Felelős kijelölése, tömeges hozzárendelés | igen | nem | nem |
| Telephely kezelése | igen | nem | nem |
| Fiókok, szerepkörök, aktiválás kezelése | igen | nem | nem |
| Saját profil és jelszó | igen | igen | igen |
| Adatbázis-visszaállítás kezdeményezése | igen, karbantartási feltételekkel | nem | nem |

A régi ügyek **kiosztatlanok** maradnak; az admin hozzárendelheti őket egy aktív
ügyintézőhöz. A letiltott fiók nem törlődik, történeti hivatkozásai megmaradnak.
Az utolsó aktív rendszergazda nem tiltható le és nem fokozható le.
A fejlécben látható az aktuális profil; a „Saját ügyeim” és a kiosztatlan ügyek
szűrője csökkenti a napi keresést. A tömeges kiosztás minden kijelölt ügyet
ellenőriz, és csak teljes siker esetén ír.

A jogosultságvizsgálat a szolgáltatásrétegben történik, nem pusztán letiltott
gombokkal. Fontos változásokhoz szereplőt, műveletet, érintett azonosítót és
időpontot tartalmazó audit tartozik, az adatváltozással egy tranzakcióban.
Jelszavak és hashértékek nem kerülnek táblázatba, exportba vagy auditba.

## Adatmegőrzés és migráció

**Frissítés előtt állítsa le az összes régi klienst**, majd készítsen ellenőrzött,
offline másolatot a teljes adatgyökérről, különösen a DB-ről és a csatolmányokról.
Az alkalmazás verziózott migrációja megőrzi az azonosítókat, a megadott
hatálytípust és az eredeti hibás felülvizsgálati forrásadatot is. Hibás vagy árva
régi adatok esetén az indulási hiba okát előbb tisztázni kell; ne hozzon létre
helyettük másik üres adatbázist.

A relatív felülvizsgálatok mindig az aktuális lejáratból számolódnak; a nulla
nap is érvényes. A kiürített felülvizsgálati lista nem éleszti újra a régi
egymezős dátumot. Hibás forrásadat nem írható felül észrevétlenül szerkesztéskor.
A mentési hiba az űrlap adatait nem törölheti. Párhuzamos módosítás esetén
ütközésjelzés után újra kell tölteni az ügyet, nem írjuk felül csendben a másik
felhasználó módosítását.

A csatolmányhozzáadás és -eltávolítás mentésig tervezet. Mégse esetén az eredeti
fájlok változatlanok; az ügy logikai törlése sem törli a csatolmánykönyvtárat.
A veszélyes kiterjesztések, túl nagy fájlok és útvonalból kitörő nevek elutasítottak,
azonos fájlnév nem írhat felül másik fájlt.
Az SQLite és a fájlrendszer nem alkot közös tranzakciót: a kezelt hibákat
kompenzáljuk, de folyamat-/gépleállás vagy második fájlrendszerhiba után
kézi egyeztetésre lehet szükség. Ilyenkor ne törölje a `.staging` tartalmát
ellenőrzés nélkül; tartalmazhat helyreállítandó eredeti csatolmányt.

## Mentés, helyreállítás és telepítési határ

Az adatbázismentés SQLite backup API-t használ, nem élő fájlmásolást.
Az elkészült ideiglenes másolat ellenőrzés után kap egyedi végleges nevet;
csak ezután történhet a régi mentések rotációja.
**A DB-mentés nem tartalmazza a csatolmányokat**, így önmagában nem teljes
katasztrófa-helyreállítás. A teljes adatgyökérhez külön, összehangolt fájlmentés
és kipróbált helyreállítási eljárás szükséges.

A visszaállítás karbantartási művelet, nem egy nyitott DB-fájl vak lecserélése.
Az ablakból indított online visszaállítás szándékosan tiltott.
Minden kliensből ki kell lépni; futó kliensek mellett a művelet megtagadott.
Az üzemeltető biztosítsa, hogy régi vagy külső program sem használja az adatbázist.
Csak ellenőrzött sémájú, integritású mentés használható, kötelező előzetes
biztonsági másolattal. Visszaállítás után új bejelentkezés szükséges.
Régi programverzióra visszatéréshez az eredeti offline adatgyökér-másolatot
használja, ne a már migrált adatbázist.

Az ellenőrzött offline helyreállító csak **helyi POSIX fájlrendszeren** támogatott,
az aktuális sémaverzió mentéséből. Az üzemeltető előbb állítson le minden klienst
és külső SQLite-eszközt, és vonja vissza más felhasználók hozzáférését az
adatkönyvtárhoz a teljes karbantartás idejére:

```bash
python -m core.db offline-restore \
  --database /srv/meh/db/engedelyek.db \
  --backup /srv/meh/backup/ellenorzott.db.bak \
  --precopy-dir /srv/meh/helyreallitas-elotti-mentes \
  --exclusive-local
```

Az `--exclusive-local` az üzemeltető nyilatkozata, **nem automatikus bizonyíték**
a helyi tárolóra vagy a külső kliensek hiányára. A program saját klienseit
megosztott/kizárólagos OS-zár koordinálja; a régi vagy külső programokat ez nem
kényszeríti együttműködésre. A művelet kötelező előmentést készít, lezárja a WAL-t,
ellenőrzi a forrást, és csak lezárt kapcsolatok után cserél adatbázist.
Visszavonja a mentésben tárolt munkameneteket és auditbejegyzést készít.
Az árva hivatkozásokat a migráció megőrzi és jelzi, a visszaállító viszont
ilyen mentést elutasít; előbb ellenőrzött adatjavítás szükséges.
Utána állítsa vissza az ACL-eket és indítsa újra a klienseket.
**Windows/SMB alatt ez az automatikus helyreállítás nem támogatott**; kizárólag
üzemeltető által biztosított offline, teljes adatgyökér-helyreállítást használjon,
vagy vigye az ellenőrzött mentést elkülönített, támogatott helyi környezetbe.
Bizonytalan kizárólagosság esetén ne állítson vissza.

A mentés és csatolmány-publikálás azonos fájlrendszeren hard link támogatást
igényel. Ha a megosztás ezt nem biztosítja, a művelet biztonságosan hibát jelez,
nem vált át felülíró másolásra. A migráció előtti mentés hibája megállítja az indulást.

Az alapértelmezett journal `DELETE`. A `WAL` csak tudatosan konfigurált helyi
tárolón használható; hálózati fájlrendszeren nem támogatott
([SQLite WAL](https://www.sqlite.org/wal.html)).
**A rollback journal sem tesz minden SMB-megosztást biztonságossá**:
a szervernek, kliensnek és fájlrendszernek megfelelő SQLite-zárolási, szinkronizálási
és tartóssági szemantikát kell biztosítania. Hálózati kapcsolatvesztés és nem
megfelelő fájlzárolás továbbra is adatvesztési kockázat. Az elsődlegesen ajánlott
üzem helyi lemez egy alkalmazáspéldánnyal; többfelhasználós erős izolációhoz
szerveroldali API és adatbázis szükséges.

**Bizalmi határ:** aki közvetlenül írhatja az SQLite-fájlt, módosíthatja a szerepeket,
jelszóhash-eket és auditot is. A belépés nem nyújt kriptográfiai védelmet vagy
szerveroldali hozzáférés-elkülönítést. Az operációs rendszer és a megosztás ACL-jeit
szűkítse megbízható felhasználókra; a mentésekre és csatolmányokra is alkalmazzon
ACL-t. Régi, jogosultságellenőrzés nélküli klienst ne használjon párhuzamosan.

## Ellenőrzés

A tesztek ideiglenes adatbázisokkal és fájlokkal futnak:

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

Linux headless Qt környezetben a `libEGL`/`libOpenGL` rendszerkönyvtárak is
szükségesek. Éles adatállományt soha ne adjon tesztnek.

### Kézi regressziós ellenőrzőlista

- [ ] Korábbi adatgyökér másolatának migrációja, ismételt indítás és azonosítók.
- [ ] Első admin, hibás jelszó, tiltott fiók, profil/jelszócsere, kijelentkezés.
- [ ] Mindhárom szereppel gombok, gyorsbillentyűk, helyi menük és tömeges műveletek.
- [ ] Telephelyek, összes kategória, hatálytípusok, lejárat és több felülvizsgálat.
- [ ] Felelős, saját/kiosztatlan szűrés, keresés, kijelölés és rendezés frissítéskor.
- [ ] Új és szerkesztett ügy, Mégse, hibás adat, párhuzamos szerkesztési konfliktus.
- [ ] Csatolmány hozzáadás/törlés/megnyitás, drag-and-drop, Mégse és fájlhiba.
- [ ] Archívum, logikai törlés, határidős listák, PDF, Excel és egyéni riport.
- [ ] Mentés és offline visszaállítás próbája DB-vel és külön csatolmánymentéssel.
- [ ] A tényleges Windows/SMB környezet zárolási, kapcsolatvesztési és ACL-próbája.

Az automatizált tesztek nem helyettesítik a Windows-felület, nyomtatás,
hálózati infrastruktúra és szervezeti mentési eljárás kézi átvételét.
