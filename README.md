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
- A `Hatályos:` vagy `Érvényes:` utáni dátum a lejáratot tölti ki.
  Példák: `2030.12.31.`, `2030-12-31`, `2030/12/31`, `2030. december 31.`
- Egyértelmű dátumtartománynál a záródátumot használja:
  `2025.01.01. – 2030.12.31.` vagy
  `2025. január 1-től 2030. december 31-ig`.
- A „határozatlan” és a „visszavonásig / változtatásig / módosításig”
  szövegeket is kezeli. Kezdődátumból vagy relatív időtartamból nem talál ki
  lejáratot.

Több fájl vagy oldal eltérő találatai esetén az érintett mezőt nem írja felül,
hanem figyelmeztet. A feldolgozás közben kézzel módosított mezők megmaradnak.
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

### Fájlok megnyitása

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
