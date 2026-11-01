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

Egy gépes használatkor a SQLite-adatbázis helyi lemezen legyen. A könyvtár a
`MEH_NETWORK_ROOT` környezeti változóval is megadható. Több számítógéphez az
alábbi **Központi, többfelhasználós üzem** beállításait használja; ilyenkor ez
a könyvtár a közösen elérhető csatolmányokat tartalmazza, nem az élő adatbázist.

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
Tesseract. **A helyi OCR és az OCR-sablonok nem küldenek dokumentumot külső
szolgáltatóhoz.** Az alábbi, külön jóváhagyást kérő **AI-kitöltés** viszont
az OpenAI API-t használja.

## Adatfelvitel és csatolmányok

Az engedély adatlapján, az **Általános** fülön található a **Fájl tallózása…**
gomb. A **Módosító határozatok** az alapadatok alatt, ugyanezen a fülön
hozzáadhatók, szerkeszthetők, törölhetők és rendezhetők. A csatolmánykezelő
az adatmezők alatt kapott helyet; a fájllista alapból összecsukott,
a **Fájlok (darabszám)** gombbal nyitható ki.

PDF, PNG, JPEG, TIFF (többoldalas is), BMP és WebP hozzáadásakor
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

### Hatóságonkénti OCR-sablonok

A csatolmánykezelő OCR-sablonválasztójában az automatikus felismerés mellett
saját hatósági dokumentumsablon választható. A **Kiadó hatóság** mezőhöz
illeszkedő profilok előre kerülnek a listában; új sablonhoz ez a hatóság
alapértelmezett. A profilok létrehozhatók, szerkeszthetők, másolhatók és
törölhetők az alkalmazásban.

Az **Új sablon…** szerkesztőben nyisson meg mintadokumentumot, válasszon
oldalt és mezőt (**Tárgy** vagy **Hatály/lejárat**), majd egérhúzással
jelölje ki a felismerési területet. Az előnézet nagyítható, PDF és
többoldalas kép is használható. A koordináták az oldal méretéhez
viszonyítva, az oldalszámmal együtt mentődnek, így eltérő felbontású, de
azonos elrendezésű szkennelt dokumentumokkal is használhatók.

A sablon a kijelölt területeket helyi OCR-rel olvassa, nem az egész oldal
szövegéből próbálja kitalálni a mezők helyét. Eltérő oldaltördeléshez vagy
áthelyezett mezőkhöz másik sablon vagy új kijelölés szükséges. Hiányzó,
ütköző vagy bizonytalan mezők nem töltődnek automatikusan. A kiválasztott
sablon egy feldolgozási menet minden fájljára vonatkozik; különböző
hatósági elrendezéseket külön menetben dolgozzon fel.

Már csatolt fájlra is alkalmazható másik sablon: nyissa ki a fájllistát,
jelölje ki a dokumentumot, válasszon sablont, és kattintson az
**Újrafelismerés** gombra. Ez nem készít új csatolmányt. A feldolgozás
közben kézzel módosított mezők továbbra is védettek.

A sablonok a Qt `AppLocalDataLocation/ocr_templates` könyvtárában,
felhasználónként helyileg tárolódnak JSON-fájlokként. A mentett profil nem
tartalmazza a mintadokumentumot vagy annak felismert szövegét.
**Mentés előtt minden OCR-eredményt ellenőrizzen.**

### AI-kitöltés OpenAI-val

Az **AI-kitöltés…** a szkennelt oldalak képét értelmezi, nem függ a Tesseract
felismerésének minőségétől és nem igényel előre kijelölt OCR-sablont.
Négy mezőhöz ad javaslatot: **kiadó hatóság, keltezés, hatály/lejárat, tárgy**.
Ez kiegészítő, választható művelet; a tallózás és az Újrafelismerés gomb
továbbra is kizárólag helyi OCR-t indít.

1. A fájllistában jelöljön ki egy PDF-et vagy képet, majd kattintson az
   **AI-kitöltés…** gombra. Az összes oldal előnézete helyben elkészül;
   ekkor még nincs külső dokumentumfeltöltés.
2. Ellenőrizze az oldalképeket. Csak külső feldolgozásra jóváhagyott,
   nem bizalmas dokumentumot használjon. A jelölőnégyzet jóváhagyása után
   a **Küldés az OpenAI-nak** gomb továbbítja a kiválasztott dokumentum
   **összes oldalképét**, nem csak a felismerendő mezőket.
3. A válaszban a mező mellett oldalszám, forrásidézet és állapot jelenik
   meg. A sorra kattintva az idézett oldalra léphet, az előnézet nagyítható.
   **Az idézet is AI által előállított javaslat, nem független bizonyíték:
   ellenőrizze az eredeti oldalon.** Bizonytalan vagy hiányzó adat nem
   vehető át automatikusan; ezeket kézzel adja meg.
4. Jelölje be egyenként az átveendő mezőket, majd válassza a
   **Kijelölt javaslatok átvétele** gombot. Alapból egyik mező sincs
   kijelölve. A közben kézzel módosított mezők megmaradnak. Ez csak az
   adatlapot tölti ki: az engedély rögzítéséhez külön **Mentés** szükséges.

A modell utasítása megkülönbözteti a kiadót az ügyféltől, a keltezést
a hatálybalépéstől és a lejáratot a felülvizsgálati határidőtől.
Kezdődátumból vagy relatív időtartamból nem kérünk kiszámított lejáratot.
A „határozatlan” és „változtatásig/visszavonásig” állapot külön kezelhető.
Hibás dátumot, érvénytelen forrásoldalt, túl hosszú szöveget vagy hiányzó
idézetet az alkalmazás elutasít; a keltezés előtti lejáratot bizonytalannak
jelöli. Ez nem teszi tévedhetetlenné az AI-t, ezért az emberi ellenőrzés
kötelező.

#### API-beállítás

Külön OpenAI API-fiók, API-kulcs és használati keret szükséges; a ChatGPT
előfizetés önmagában nem biztosít API-keretet. Központi módban **csak a
szervergépen**, a szolgáltatás indítása előtt állítsa be:

```powershell
$env:OPENAI_API_KEY = "<OpenAI API-kulcs>"
$env:MEH_OPENAI_MODEL = "gpt-4.1"
```

Ezután ugyanabban a környezetben indítsa a `python -m core.db_service ...`
szolgáltatást. Egygépes módban a változókat a `python main.py` indítása
előtt, a helyi gépen kell beállítani. A `MEH_OPENAI_MODEL` elhagyható,
alapértéke `gpt-4.1`; más modellnek a Responses API képbevitelét és a
szigorú JSON-séma szerinti választ is támogatnia kell. A tényleges
modellhozzáférés az API-fióktól függ.

Az API-kulcs nem kerül az adatbázisba, csatolmányba vagy sablonba, és
központi módban nem adjuk át a klienseknek. Ne tegye a kulcsot a közös
mappába, forráskódba vagy verziókezelt indítófájlba. A központi szolgáltatás
AI-végpontjához ugyanaz a hitelesített HTTPS-kapcsolat szükséges, mint az
adatkezeléshez. A szolgáltatást futtató gépen a projekt függőségei is
legyenek telepítve; az oldalképek ellenőrzéséhez Pillow szükséges.

#### Adatkezelés, korlátok, hibák

A kliens memóriában JPEG-oldalképeket készít (legfeljebb 2400 pixel hosszú
oldal); az eredeti fájlt nem módosítja. Egy kérés egy dokumentum, legfeljebb
20 oldal, oldalanként 2 MB, összesen 16 MB képadat. Ha bármely korlátot
túllépi, nem küldünk csonka dokumentumot: hibaüzenetet kap. Többoldalas TIFF
és szkennelt PDF is támogatott. A teljes oldal feldolgozása használati
díjjal járhat; állítson be költségkeretet az OpenAI-projektben.

Az API-kérés `store: false` beállítású, nem használ fájlfeltöltési
végpontot, és az alkalmazás nem naplózza a dokumentumot, az AI-válasz
tartalmát vagy az API-kulcsot. **Ez nem jelent garantált nulla megőrzést
az OpenAI oldalán**: a szolgáltató adatkezelési szabályait és a fiók
beállításait az üzemeltetőnek külön jóvá kell hagynia.

Hiányzó kulcs, szolgáltatói elutasítás, kvótahiba, hálózati hiba vagy
érvénytelen válasz esetén az adatlap nem változik. Nincs automatikus
újraküldés, mert annak újabb díja lehet. A hálózati várakozás időkorlátos;
megszakításkor az ablak megvárja a folyamat végét, de az elküldött külső
kérés már nem vonható vissza. Központi módban legfeljebb két AI-feldolgozás
fut egyszerre; a harmadik felhasználó egyértelmű foglaltsági üzenetet kap,
az adatbázis-műveletek ettől továbbra is használhatók.

## Kimutatáskészítő

A Kimutatáskészítőben a telephely- és engedélytípus-szűrés mellett kiadó
hatóságra, szövegre, hatályállapotra és dátumtartományra is szűrhet.
Az archivált tételek bevonása külön választható. Az üres kijelölés nem
jelenti automatikusan az összes tételt.

Az oszlopok kiválaszthatók és húzással vagy a sorrendező gombokkal
átrendezhetők. Az előnézet ugyanazt a szűrést, rendezést és mezőformázást
használja, mint az Excel/PDF-export. Az előnézet legfeljebb nyolc sort,
illetve csoportot mutat; az összefoglaló jelzi a teljes export mennyiségét.
Az export nem csak az előnézetben látható első sorokat tartalmazza.
A megnyitott kimutatás az adatokról nyitáskor készített pillanatképet
használja, így egy másik felhasználó közben végzett mentése nem változtatja
meg észrevétlenül az előnézethez képest az exportot. Új adatokhoz zárja be
a kimutatást, frissítse a főlistát, majd nyissa meg újra.

A beállítások névvel sablonként menthetők, később visszatölthetők és
törölhetők; meglévő sablon felülírása megerősítést kér. A kimutatássablonok
felhasználónként helyileg, a Qt `GenericDataLocation` könyvtárának
alkalmazásnév szerinti almappájában, `report_templates.sqlite3` fájlban
tárolódnak. Nem közös adatbázis-adatként szinkronizálódnak a gépek között.

PDF-exporthoz Unicode-képes betűkészlet szükséges a magyar ékezetekhez:
DejaVu, Liberation, FreeSans vagy Arial. Ha egyik sem telepített,
az alkalmazás hibaüzenetet ad; telepítsen egyet, majd ismételje meg az
exportot. A csoportonként külön készülő fájlok nevei nem ütköznek egymással.

## Adatbiztonság

Az adatbázis-műveletek hiba esetén visszagörgetik a tranzakciót. A biztonsági
mentés és visszaállítás a SQLite backup API-ját használja, így a WAL-ban lévő
mentett adatok is bekerülnek a másolatba. Frissítés előtt készítsen biztonsági
mentést, és őrizze meg a csatolmánykönyvtár másolatát is: az adatbázismentés
önmagában nem tartalmazza a csatolmányokat.

A meglévő SQLite WAL-üzemmód **nem alkalmas több számítógép egyidejű,
hálózati fájlmegosztáson keresztüli adatbázis-hozzáférésére**.

## Központi, többfelhasználós üzem

2–3 felhasználó külön számítógépről a központi szolgáltatáson keresztül
dolgozhat. Az egyik, folyamatosan bekapcsolt gép futtatja a szolgáltatást;
**csak ez a gép nyitja meg a saját helyi lemezén lévő SQLite-adatbázist**.
A csatolmányok maradhatnak a közös hálózati mappában. A kliensek ugyanazt a
szolgáltatást és csatolmánykönyvtárat használják, saját helyi telepítéssel.

### Átállás és szerverindítás

1. Zárja be az alkalmazást minden gépen. Készítsen mentést a régi alkalmazás
   mentés funkciójával, és külön másolatot az `attachment` könyvtárról.
   Futó SQLite-adatbázisból ne csak a `.db` fájlt másolja ki, mert a WAL-ban
   további adatok lehetnek.
2. A létrehozott, önálló `.db.bak` mentést másolja a szerver **helyi**
   adatkönyvtárába `engedelyek.db` néven, például
   `C:\MEH-data\engedelyek.db`. Üres adatkönyvtár esetén új nyilvántartás
   készül. A régi alkalmazásokat ne indítsa újra a régi adatbázison.
3. Kérjen TLS-tanúsítványt és privát kulcsot a szervergép DNS-nevéhez
   (a név a tanúsítvány SAN mezőjében is szerepeljen). A kliensnek bíznia
   kell a kibocsátóban. Belső CA esetén annak nyilvános PEM-tanúsítványát
   adja meg a kliensen; a **privát kulcs kizárólag a szerveren marad**.
4. Generáljon véletlen hozzáférési kulcsot, például a
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` paranccsal.
   Legalább 32 karakter szükséges. A kulcsot biztonságos csatornán adja át
   a jogosult felhasználóknak; ne írja forráskódba vagy verziókezelt fájlba.
5. Indítsa a szolgáltatást a projekt könyvtárából, például PowerShellben:

```powershell
$env:MEH_SERVER_TOKEN = "<az előbb generált kulcs>"
python -m core.db_service --data-dir "C:\MEH-data" --host 0.0.0.0 --port 8765 --cert "C:\MEH-tls\server.crt" --key "C:\MEH-tls\server.key"
```

A folyamat előtérben fut; a konzol bezárása leállítja. Tartós üzemhez az
üzemeltető állítson be Windows-szolgáltatást/felügyelt indítást vagy
Linuxon systemd-egységet, azonos környezeti változókkal és helyi
adatkönyvtárral. Egyetlen szolgáltatáspéldányt futtasson. A tűzfalon csak
a szükséges belső kliensgépeknek engedje a 8765/TCP portot; ne tegye
közvetlenül elérhetővé az interneten. Titkosítatlan HTTP csak
`127.0.0.1`/`localhost` címen, helyi fejlesztési próbához engedélyezett.

### Kliensbeállítás minden felhasználó gépén

```powershell
$env:MEH_SERVER_URL = "https://meh-szerver:8765"
$env:MEH_SERVER_TOKEN = "<ugyanaz a hozzáférési kulcs>"
$env:MEH_SERVER_CA_FILE = "C:\MEH-tls\ca.crt"
$env:MEH_NETWORK_ROOT = "\\fajlszerver\megosztas\MEH"
python main.py
```

A gépnevet és útvonalakat a saját környezethez igazítsa. A
`MEH_SERVER_CA_FILE` elhagyható, ha a kibocsátó már megbízható a Python
tanúsítványtárában. A fenti megosztás `attachment` alkönyvtára legyen a
korábban használt csatolmánykönyvtár. A mappa és a `.locks` alkönyvtár
írását minden jogosult felhasználónak engedélyezni kell. Ellenőrizze,
hogy az állapotsor **Központi adatbázis** módot jelez; `MEH_SERVER_URL`
nélkül a program a régi, helyi adatkezelést használja.

### Párhuzamos szerkesztés és hibakezelés

A lista központi módban 15 másodpercenként frissül (aktív keresés közben
nem zavarja a gépelést); a kézi frissítés azonnal kérhető. A módosítások,
archiválások, törlések és telephelyváltozások a többi gépen is megjelennek.
Ugyanazt a tételt egyszerre szerkesztve az első mentés sikerül, a későbbi
elavult mentés **ütközési hibaüzenetet** kap. A nyitott adatlap beírt adatai
megmaradnak: másolja ki a szükséges módosításokat, zárja be, majd frissítés
után nyissa meg újra. A program nem írja felül automatikusan más munkáját.
A tömeges állapotmódosítás egyetlen tranzakció: ütközéskor az egész
kijelölt művelet visszagördül.

Kapcsolathibánál nincs helyi adatbázisra történő automatikus átállás.
A korábban betöltött lista megmarad, az állapotsor jelzi, hogy elavult
lehet. Ha a kapcsolat mentés közben szakad meg, a válasz elveszhetett
már sikeres mentés után is: újrapróbálás előtt ellenőrizze a központi
listát. A csatolmányok és az adatbázis külön mentési lépést használnak;
a fájlművelet hibáját külön jelzi az alkalmazás.

A végleges fájlmásolásokat megosztott zárolás védi, a méretkorlátot
mentéskor újra ellenőrzi. Törölt engedélyek csatolmánykönyvtára
helyreállítási célból megmarad; archiválást használjon, ha a rekordot is
láthatóan meg szeretné őrizni. A mentések a szerver helyi `backup`
könyvtárába kerülnek. Központi módban kliensről visszaállítás nem végezhető:
ehhez állítsa le az összes klienst és a szolgáltatást, készítsen külön
mentést az aktuális állapotról, majd a szerveren használja a SQLite
backup API-ját (a `Database.restore_backup` metódust). Ezután indítson
új munkamenetet minden kliensen. Az adatbázismentés továbbra sem
tartalmazza a közös csatolmányokat.

A hozzáférési kulcs közös alkalmazás-hozzáférést ad, nem személyenkénti
bejelentkezés vagy jogosultságkezelés. A csatolmányok hozzáférését a
fájlmegosztás jogosultságai szabályozzák.

## Ellenőrzés

```sh
python -m unittest discover -s tests -v
```

A tesztek ideiglenes adatkönyvtárat és képernyő nélküli Qt-felületet használnak.
A valódi OCR-tesztekhez a Tesseract és a magyar nyelvi csomag is szükséges.
