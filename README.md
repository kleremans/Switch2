# Zelda Switch 2 voorraad-monitor 🗡️🛡️

Doorzoekt elk uur (07:00–23:00) webshops in Nederland, Duitsland en België op de **Nintendo Switch 2 – The Legend of
Zelda 40th Anniversary Edition** (en de **Ocarina of Time Limited Edition**) en stuurt je direct een pushmelding op je telefoon
zodra hij ergens op voorraad is.

Draait gratis op GitHub Actions, dus je computer hoeft niet aan te staan.

## Hoe werkt het?

1. Op elke webshop in `config.yaml` wordt gezocht op "switch 2 zelda" en
   "zelda 40th anniversary".
2. Links in de zoekresultaten die op de console lijken worden verzameld.
   Controllers, tassen, hoesjes, games (zoals *Breath of the Wild – Switch 2 Edition*)
   e.d. worden eruit gefilterd.
3. Elke gevonden productpagina wordt gecontroleerd op voorraad, eerst via de
   gestructureerde productgegevens (schema.org `InStock`/`OutOfStock`) en anders via
   teksten als "uitverkocht" of "in winkelwagen".
4. Gaat een product van *uitverkocht* naar *beschikbaar*, dan krijg je een melding.
   De status wordt bijgehouden in `state.json`, zodat je niet elke run opnieuw
   dezelfde melding krijgt.

Gecontroleerde shops (volledige lijst en instellingen in `config.yaml`):

- 🇳🇱 Coolblue, bol.com, MediaMarkt, Amazon.nl, Nintendo, Intertoys, Game Mania,
  Nedgame, Wehkamp, EP, Alternate, Tweakers Pricewatch
- 🇩🇪 Amazon.de, MediaMarkt.de, Saturn, Otto, Coolblue.de, Galaxus, Alternate.de,
  Müller, Smyths Toys, GameStop, Geizhals (prijsvergelijker)
- 🇧🇪 Coolblue.be, MediaMarkt.be, Amazon.com.be, Fnac, Vanden Borre, Krëfel,
  Dreamland, Game Mania BE

### Indicaties in elke melding

- **Land** met vlag (🇳🇱/🇩🇪/🇧🇪) en of de shop **naar Nederland verzendt**
  (ja / nee / onbekend, met toelichting zoals "afhalen in BE kan").
- **Prijs incl. verzending**: bij buitenlandse shops die naar NL verzenden wordt
  een indicatie van de verzendkosten (`shipping_nl` in `config.yaml`) opgeteld.
  De prijsgrens geldt voor dat totaal.

### Vroege signalen 👀

Naast "op voorraad" (🚨, prioriteit urgent) krijg je een melding met prioriteit
hoog als:

- een shop meldt dat de console **binnenkort (weer) leverbaar** is
  ("binnenkort beschikbaar", "demnächst verfügbar", "bientôt disponible", …);
- er een **nieuwe productpagina** opduikt bij een shop die al gevolgd werd.

Uitzetten: `early_signals: enabled: false` in `config.yaml`.

### Andere edities volgen

Onder `match: variants:` in `config.yaml` staan extra edities, zoals de Ocarina of
Time Limited Edition. Elke variant heeft eigen verplichte woorden; een
"console-woord" voorkomt dat de gelijknamige game meetelt.

### Prijsgrens

Doorverkopers bieden de console vaak aan voor €750+. Je krijgt alleen een melding
als de prijs **onder `max_price`** ligt (standaard €650, in `config.yaml`). Duurder
aanbod staat in de log als "💸 te duur".

### Tweakers Pricewatch

Veel grote shops (bol.com, MediaMarkt, Amazon, Nedgame, Game Mania) blokkeren
GitHub. Tweakers Pricewatch verzamelt de prijzen van al die winkels wél, dus als
een van hen de console weer verkoopt, ziet de monitor dat via Tweakers.

### Sociale media (tips van anderen)

De monitor leest ook mee op **Pepper.com NL**, **Reddit** en **Bluesky**. Meldt
iemand daar dat de Zelda Switch 2 ergens (in Nederland) op voorraad is, dan krijg
je een melding "💬 Tip via …" met een link naar de post. Zulke tips zijn niet
gecontroleerd, dus kijk zelf even. Ze komen met prioriteit "hoog" binnen; een
door de monitor zelf gevonden voorraad met prioriteit "urgent".

- Bij Reddit en Bluesky moet de post over Nederland gaan (een NL-shop of
  "Nederland" noemen), anders krijg je ook alle Amerikaanse restocks.
- Bij de eerste run worden bestaande posts alleen onthouden, niet gemeld.
- Maximaal 3 tips per run. Instellingen staan onder `social:` in `config.yaml`;
  uitzetten met `enabled: false`.
- **Reddit en Bluesky** blokkeren anonieme verzoeken vanaf GitHub. Wil je ze
  gebruiken, voeg dan (gratis) een sleutel toe als repository-secret:
  - *Reddit*: maak op https://www.reddit.com/prefs/apps een app van het type
    **script** (redirect-URI mag `http://localhost` zijn). Zet de code onder de
    app-naam in `REDDIT_CLIENT_ID` en het "secret" in `REDDIT_CLIENT_SECRET`.
  - *Bluesky*: maak in de app via Instellingen → Privacy en beveiliging →
    App-wachtwoorden een app-wachtwoord. Zet je handle (bv. `naam.bsky.social`) in
    `BLUESKY_HANDLE` en het app-wachtwoord in `BLUESKY_APP_PASSWORD`.
- X/Twitter, Instagram, Facebook en TikTok kunnen niet: die vereisen een betaalde
  API of een ingelogd account.

## Installatie (±5 minuten)

### 1. Meldingen op je telefoon (ntfy, gratis, geen account nodig)

1. Installeer de app **ntfy** ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) /
   [iPhone](https://apps.apple.com/app/ntfy/id1625396347)).
2. Verzin een lange, unieke onderwerpnaam, bijvoorbeeld `zelda-switch2-k8f3q9x2m`.
   (Iedereen die de naam kent kan meelezen, dus maak hem niet te raden.)
3. Tik in de app op **+** en abonneer je op die naam.
4. Ga in GitHub naar **Settings → Secrets and variables → Actions → New repository secret**
   en maak het secret `NTFY_TOPIC` aan met die naam als waarde.

*Optioneel: Telegram.* Maak een bot via @BotFather en voeg de secrets
`TELEGRAM_BOT_TOKEN` en `TELEGRAM_CHAT_ID` toe.

*Extra vangnet:* bij een treffer wordt ook een GitHub-issue aangemaakt, waarvan je
een mail of GitHub-app-melding krijgt. Uitzetten: maak de repository-variabele
`GITHUB_ISSUE_ALERTS` met waarde `false`.

### 2. Activeren

Geplande GitHub Actions draaien **alleen vanaf de standaardbranch** (`main`).
Zorg dat deze bestanden op `main` staan (merge de branch).

### 3. Testen

Ga naar **Actions → Zelda Switch 2 monitor → Run workflow**:

- vink **Alleen een testmelding sturen** aan → je telefoon moet afgaan;
- run nog eens zonder vinkje → in de log zie je per shop of de zoekpagina bereikbaar
  was (HTTP 200) en welke productpagina's zijn gevonden.

## Tijden

Elk uur tussen 07:00 en 23:00 Nederlandse tijd: **07:17, 08:17, … 22:17** (16x per
dag), het hele jaar door (zomer- en wintertijd worden automatisch goed gezet).

Hoe: GitHub rekent alleen in UTC, dus de workflow start elk uur tussen 05 en 21 UTC
en de eerste stap ("Tijdvenster") laat alleen de bovenstaande Nederlandse uren door.
De overige start stopt meteen. Andere uren nodig? Pas de lijst `07|08|...|22` in
`.github/workflows/monitor.yml` aan.

> 💡 Restocks zijn vaak binnen minuten weer uitverkocht. Hoe vaker je controleert,
> hoe groter de kans dat je op tijd bent. Voor een publieke repository zijn Actions
> gratis; bij een privé-repository heb je 2000 gratis minuten per maand, en dit
> schema gebruikt er ongeveer 1.500 (een run duurt nu ±2,5 minuut).

## Bekende beperkingen

- Sommige grote shops (o.a. bol.com, Amazon, MediaMarkt) blokkeren soms verzoeken
  vanaf servers van GitHub. Je ziet dat in de log als HTTP 403/503. De tool gebruikt
  `curl_cffi` om zich als een gewone Chrome-browser voor te doen, wat veel helpt maar
  niet altijd. Shops die je toch niet kan bereiken, kun je uitzetten met
  `enabled: false`.
- Vind je zelf een productpagina van de Zelda-editie, zet hem dan onder
  `product_urls` in `config.yaml`. Die worden altijd gecontroleerd, ook als de
  zoekfunctie van de shop niet meewerkt.
- GitHub zet geplande workflows uit na 60 dagen zonder activiteit in de repository.
  Omdat de monitor elke run `state.json` bijwerkt, blijft hij actief.

## Lokaal draaien

```bash
pip install -r requirements.txt
NTFY_TOPIC=jouw-onderwerp python monitor.py --test-notify
python monitor.py --verbose
```
