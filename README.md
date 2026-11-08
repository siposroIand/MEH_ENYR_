# MEH engedély-nyilvántartó

Python 3.10+ alapú, PySide6 asztali alkalmazás.

## Telepítés és indítás

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

A `config/app_config.py` fájl `NETWORK_ROOT` beállításában adja meg az
adatkönyvtárat. Az alkalmazás itt tárolja az adatbázist, a csatolmányokat és a
biztonsági mentéseket. A könyvtárhoz írási jogosultság szükséges.

### Helyi OCR

A képekhez és szkennelt PDF-ekhez külön **Tesseract OCR** telepítés szükséges,
a **magyar (`hun`) nyelvi adatokkal** együtt. Debian/Ubuntu rendszeren:

```sh
sudo apt-get install tesseract-ocr tesseract-ocr-hun
```

Windows alatt a Tesseract telepítésekor válassza ki a magyar nyelvet, és tegye
a programot a `PATH` változóba, vagy indítás előtt állítsa be az elérési útját
PowerShellben:

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
python main.py
```

A telepítés a `tesseract --list-langs` paranccsal ellenőrizhető: szerepeljen a
listában a `hun`. A szöveges PDF-részek közvetlen olvasásához nem szükséges
Tesseract. **A dokumentumok nem kerülnek külső OCR-szolgáltatóhoz.**

## Adatfelvitel és csatolmányok

Az alapadatok, a megjegyzés, a hatály, a felülvizsgálatok és a csatolmányok
kompakt, kétoszlopos **Általános** fülön találhatók. Az alapűrlap
1024×640-es ablakméretnél görgetés nélkül használható; kisebb ablaknál a
görgetési lehetőség megmarad. A **Módosító határozatok** külön fülre kerültek,
a hulladékadatok továbbra is a **Hulladék** fülön szerkeszthetők.

Az engedély adatlapján, az **Általános** fülön található a **Fájl tallózása…**
gomb. PDF, PNG, JPEG, TIFF (többoldalas is), BMP és WebP hozzáadásakor
automatikusan elindul a háttérben a szövegfelismerés:

- A `Tárgy:` utáni szöveg a tárgymezőbe kerül; több sor esetén a következő
  mezőig vagy üres sorig tartó szöveget használja.
- A `Hatályos:`, `Érvényes:`, `Lejárat:`, `Hatálya:` és
  `Érvényességi idő:` mezőkből, illetve az engedély/határozat hatályáról szóló
  mondatokból keresi a lejáratot. Például `2026.november 19.` → `2026.11.19.`,
  illetve `Az engedély 2026. november 19. napjáig érvényes.`
  A numerikus `2030.12.31.`, `2030-12-31` és `2030/12/31` alak is támogatott.
- A `kiadástól 5 évig`, `kiadásától számított öt év` és hasonló szövegekhez
  megkeresi a dokumentum keltezését (`Kelt:`, `Keltezés:`, kiadási dátum vagy
  `Budapest, 2021. november 19.` jellegű dátumsor), és naptári éveket ad hozzá.
  A keltezés másik oldalon is lehet, de másik csatolmányból nem veszi át.
  Február 29. nem szökőévre számítva február 28. lesz.
- A `Határozatszám`, `Határozat száma`, `Ügyiratszám`, `Iktatószám`,
  `Ügyszám`, `Engedély száma` és `Szám` fejléceket is felismeri.
  Nem követel egyetlen számalakot: betűs előtagok, szóközök, pontok,
  kötőjelek és perjelek is megmaradnak, például `PE/KTFO/1234-5/2026`.
- Egyértelmű dátumtartománynál a záródátumot használja:
  `2025.01.01. – 2030.12.31.` vagy
  `2025. január 1-től 2030. december 31-ig`.
- A „határozatlan” és a „visszavonásig / változtatásig / módosításig”
  szövegeket is kezeli. Kezdődátumból önmagában nem talál ki lejáratot.
  Hiányzó, hibás vagy többféle keltezésnél nem számol relatív lejáratot.
  Jogerőre emelkedést, kézbesítést vagy véglegessé válást nem helyettesít
  automatikusan a keltezéssel.

Több fájl vagy oldal eltérő találatai esetén az érintett mezőt nem írja felül,
hanem figyelmeztet. A feldolgozás közben kézzel módosított mezők megmaradnak.
Ez a határozatszámra is érvényes. A felismerés helyi, szabályalapú segítség,
nem minden határozatsablonra garantált értelmezés.
**Mentés előtt ellenőrizze az OCR-rel felismert adatokat.** Hiányzó OCR-motor,
hibás dokumentum vagy időtúllépés esetén a csatolmány megmarad és az adatok
kézzel is megadhatók. Az OCR időkorlátja képenként 45 másodperc; megszakításkor
az aktuális felismerés befejezését megvárja, az ablak közben válaszképes marad.

### Saját OCR-szabályok

A főablak **Beállítások → OCR-szabályok…** menüje és az adatlap
**OCR-szabályok…** gombja ugyanazt a beállítást nyitja meg. A tárgyhoz és az
érvényességhez külön-külön adható meg:

- **Kezdő kifejezés**, például `Tárgy` vagy `Érvényes`: az utána következő
  szöveget olvassa a következő mezőig vagy üres sorig.
- **Kezdő és záró kifejezés**, például `Érvényes` és `Indokolás`: a két
  kifejezés közötti értéket olvassa az adott oldalon.
- Üres kezdő és záró kifejezésnél az adott mező beépített felismerése marad
  érvényben. A **Beépített felismerés** gomb mindkét mezőt visszaállítja;
  a változást a **Mentés** gomb véglegesíti.

A keresés szó szerinti, nem reguláris kifejezés; a kis- és nagybetűk, illetve
a szóközök/sortörések különbsége nem számít. A saját szabály csak az adott
mező beépített szabályát helyettesíti. Hiányzó záró kifejezésnél vagy eltérő
találatoknál figyelmeztet; az érvényességhez továbbra is egyértelmű dátum vagy
hatályszöveg szükséges.

A mintaszöveg mező és a **Kipróbálás** gomb mentés nélkül mutatja a várható
eredményt. A mentett szabályok a helyi felhasználó Qt-beállításaiban
(`MEH/EngedelyNyilvantarto`) tárolódnak, újraindítás után is megmaradnak;
nem kerülnek a közös adatbázisba. Az **OCR újra** gomb a kijelölt csatolmányt,
kijelölés nélkül az adatlap összes támogatott csatolmányát újra feldolgozza
az aktuális szabályokkal, újabb fájlmásolat készítése nélkül.

### Engedélytípusok és hatóságok

Az **Engedélytípus** mező szerkeszthető: az alaplista mellett saját kategória
is megadható. A mentett kategóriák a későbbi adatfelvitelnél, szűrésnél és
csoportos exportálásnál is elérhetők. Excel-exportnál a munkalapnevek
érvényesek és egyediek maradnak hosszú vagy perjeles kategóriáknál is;
az adatcellákban az eredeti név szerepel. A kiadó hatóság mező a mentett
hatóságnevekből kínál kiegészítést. Az új nevek az engedéllyel együtt,
egyetlen tranzakcióban kerülnek a törzstáblákba; hibás mentés nem hagy
félkész törzsadatot.

## Adatbázis és átállás a régi verzióról

Az új SQLite-adatbázis pontosan négy táblát tartalmaz:

| Tábla | Tartalom |
|---|---|
| `telephelyek` | Telephelyek, KTJ és cím |
| `engedelyek` | Engedélyadatok, hatály, felülvizsgálatok, hulladékadatok |
| `hatosagok` | Egyedi hatóságnevek |
| `engedelytipusok` | Egyedi engedélytípusok/kategóriák |

Az engedélyek idegen kulccsal hivatkoznak a telephelyre, a hatóságra és az
engedélytípusra. A kapcsolatok ellenőrzése minden kapcsolaton aktív.
A kapcsolatok és az időbélyeg szerinti részleges frissítés indexeltek;
az alkalmazás indításkor nem végez teljes táblás adatjavítást.
Foglalt adatbázisnál a SQLite zárolásvárakozási időkorlátja 2 másodperc, az
újrapróbálható olvasások legfeljebb három kísérletet tesznek.
A dátumok belül ISO (`YYYY-MM-DD`) alakban maradnak; az adatlap a
keltezést és lejáratot `YYYY.MM.DD.` alakban mutatja.

**Nincs automatikus migráció.** Régi adatbázisnál az alkalmazás érthető
hibával leáll, nem módosítja a fájlt. Az egyszeri konvertálást a projekt
gyökerében található **`convert_database.py`** végzi. A projektből futtatható,
csak a Python standard könyvtárát és a projekt közös sémadefinícióját
használja; a GUI/OCR csomagokat nem kell hozzá telepíteni.

1. Zárja be az alkalmazás összes példányát. Készítsen biztonsági mentést
   az adatbázisról és a csatolmánykönyvtárról.
2. A projekt könyvtárából futtassa, két különböző fájlnevet megadva:

   ```sh
   python convert_database.py "regi.db" "uj.db"
   ```

3. Az új fájl ellenőrzése után állítsa be a `config/app_config.py`
   `DB_FILENAME` értékét az új fájlnévre, és helyezze a fájlt az adatkönyvtár
   `db` almappájába. A régi fájlt őrizze meg. A csatolmányok helye változatlan.

A konvertáló új fájlt készít, meglévő célt nem ír felül. A mentett WAL-adatokat
is beolvassa, megőrzi az engedély- és telephelyazonosítókat, az archivált és
törölt engedélyeket, valamint a meglévő felülvizsgálatokat. A régi hiányzó
hatály- és felülvizsgálati mezőket egyszer tölti fel. A hatóság- és
kategórianevekről levágja a szélső szóközöket, az azonos neveket összevonja.
Aktív engedély hiányzó telephelye, ismeretlen oszlop vagy sérült adatbázis
esetén leáll, kész célfájl nélkül. A korábban törölt engedély már nem létező
telephelyhivatkozását figyelmeztetéssel kiüríti, az engedélyt megtartja.

Az új fájlt csak sikeres integritás-, idegenkulcs- és rekordszám-ellenőrzés
után teszi elérhetővé. Ehhez a célkönyvtár fájlrendszerének hard linket kell
támogatnia (például helyi NTFS vagy ext4); hálózati megosztás helyett
érdemes helyi célfájlba konvertálni, és utána átmásolni a kész adatbázist.
A régi `db_events` eseménynapló a régi fájlban marad; az új alkalmazás
eseményei a Python-naplóba kerülnek. Régi sémájú biztonsági mentést is előbb
konvertálni kell, közvetlenül nem állítható vissza az új alkalmazásban.

## Fájlok megnyitása

A főtábla **Engedély(ek)** oszlopán dupla kattintással, illetve a jobb gombos
**Csatolmányok megnyitása** menüponttal nyithatók meg a fájlok.
Egy csatolmánynál a fájl, több csatolmánynál a tartalmazó mappa nyílik meg.
Az adatlapon a fájllista dupla kattintása és a **Megtekintés** gomb is
használható; egyetlen fájlnál nem szükséges külön kijelölés.

A képek az alkalmazás előnézetében, a többi fájl az operációs rendszer
alapértelmezett alkalmazásában nyílik meg. Ehhez telepített és hozzárendelt
megjelenítő (például PDF-olvasó) szükséges. Hiányzó fájlnál vagy a megnyitási
kérés elutasításánál hibaüzenet jelenik meg. A megnyitás helyi fájl-URL-t
használ, így a szóközök, ékezetek és speciális karakterek megmaradnak.

Más fájltípus is csatolható, de automatikus felismerés nélkül. A korlátok
a `config/settings.py` fájlban találhatók: alapértelmezetten 50 MB/fájl,
500 MB/engedély, valamint tiltott futtatható kiterjesztések. Ugyanezeket
ellenőrzi a főablak meglévő drag-and-drop csatolmányimportja is; az továbbra
is közvetlen csatolás, nem OCR-es adatfelvitel.

A tallózott fájlok és a kijelölt törlések csak az engedély sikeres mentése után
véglegesülnek. A **Mégse** eltávolítja az adott adatlap ideiglenes másolatait,
és nem törli a meglévő csatolmányokat. Azonos fájlnevek nem írják felül egymást.
Adatbázishibánál az adatlap újra megnyílik, az adatok javíthatók és újra
menthetők. Ha az adatbázis mentése sikerül, de a fájlművelet nem, külön
figyelmeztetés és újrapróbálási lehetőség jelenik meg. Megszakított újrapróbálás
után a jelzett ideiglenes könyvtár fájljait kézzel kell rendezni.

## Lejárati figyelmeztetések és frissítés

Indulás után automatikus felugró ablak jelzi a már lejárt, a **0–90 napon**
belül és a **91–180 napon** belül lejáró engedélyeket. Az ellenőrzés az
ötpercenkénti adatfrissítés és a kézi **Frissítés / F5** után is lefut,
a táblázat keresésétől és szűrőitől függetlenül. Archivált, törölt,
határozatlan és változtatásig érvényes tételekre nem figyelmeztet.

Ugyanazt az engedélyt ugyanazzal a lejárattal és figyelmeztetési kategóriával
egy futáson belül naponta egyszer jelzi. Új tétel, megváltozott lejárat,
új nap vagy újraindítás ismét kiválthatja az ablakot. Nyitott szerkesztőablak
esetén a következő ellenőrzésre halasztja az értesítést; a már nyitott
figyelmeztetésből nem készít újabb példányt. A **Lejáró engedélyek** gombbal
a lista bármikor megnyitható. Bezárt alkalmazás nem küld értesítést.

A táblázat frissítése és keresése nem járja végig a csatolmánymappákat:
a **Megnyitás** felirat nem jelent hiányzó csatolmányt, a darabszám megnyitáskor
frissül. Adatbázishibánál a korábbi adatok és a frissítési pont megmaradnak,
az állapotsor hibát jelez. A következő sikeres frissítés a közben történt
törléseket is átveszi.

## Adatbiztonság

Az adatbázis-műveletek hiba esetén visszagörgetik a tranzakciót. A biztonsági
mentés és visszaállítás a SQLite backup API-ját használja, így a WAL-ban lévő
mentett adatok is bekerülnek a másolatba. Frissítés előtt készítsen biztonsági
mentést, és őrizze meg a csatolmánykönyvtár másolatát is: az adatbázismentés
önmagában nem tartalmazza a csatolmányokat.

A meglévő SQLite WAL-üzemmód **nem alkalmas több számítógép egyidejű,
hálózati fájlmegosztáson keresztüli adatbázis-hozzáférésére**. Ilyen üzemben
központi adatbázis-szolgáltatásra vagy külön tárolási átalakításra van szükség;
ezt az OCR-fejlesztés nem változtatja meg.

## Ellenőrzés

```sh
python -m unittest discover -s tests -v
```

A tesztek ideiglenes adatkönyvtárat és képernyő nélküli Qt-felületet használnak.
A valódi OCR-tesztekhez a Tesseract és a magyar nyelvi csomag is szükséges.
