# voice separator

Extrait les voix Simple Voice Chat d'un replay [Flashback](https://modrinth.com/mod/flashback) en une piste WAV par joueur.

Les pistes sont **brutes** : pas d'atténuation par la distance, pas de diaphonie entre joueurs. Chaque fichier ne contient que le micro de son locuteur, sur toute la durée du replay, aligné sur t=0.

## Prérequis

Python 3.8 ou plus. Aucune dépendance à installer, tout est en bibliothèque standard.

```bash
python --version
```

## Utilisation

Poser le dossier du replay dans `input/`, puis :

```bash
python extract_voices.py
```

Sans argument, le script prend le premier replay trouvé dans `input/` et écrit dans `output/<nom du replay>/`.

Le dossier de replay attendu est celui que produit Flashback, c'est-à-dire un dossier contenant `metadata.json` et les fichiers `c0.flashback`, `c1.flashback`, etc. Flashback les écrit dans le sous-dossier `flashback/replays/` du dossier de jeu — `%APPDATA%\.minecraft\` par défaut, ou le dossier de l'instance si le lancement se fait par Prism, Modrinth ou CurseForge.

## Options

| Option | Défaut | Effet |
|---|---|---|
| `--replay <dossier>` | premier dossier de `input/` | Dossier du replay à traiter |
| `--out <dossier>` | `output` | Dossier de sortie |
| `--chunks <N>` | tous | Ne traiter que les N premiers chunks |
| `--tolerance-ms <ms>` | `100` | Écart au-delà duquel un silence est inséré |

### Traiter un replay précis

```bash
python extract_voices.py --replay "C:\Users\FlowUP\AppData\Roaming\.minecraft\flashback\replays\2026-08-15T17_04_28"
```

### Test rapide avant de lancer sur tout

Un chunk vaut 5 minutes de replay. Pour vérifier la sortie sans traiter des heures :

```bash
python extract_voices.py --chunks 1 --out output_test
```

### Choisir le dossier de sortie

```bash
python extract_voices.py --out "D:\montage\pistes voix"
```

### Ajuster la resynchronisation

`--tolerance-ms` fixe l'écart maximal toléré entre l'horloge du replay et la fin de la piste avant d'insérer un silence. En dessous de cet écart, les trames voix sont collées bout à bout, ce qui garde la parole continue.

- Valeur **plus basse** (ex. `50`) : recalage plus fréquent, meilleure synchro avec l'image, mais risque de micro-coupures dans les phrases.
- Valeur **plus haute** (ex. `250`) : parole plus fluide, léger flottement possible sur les longues tirades.

La valeur par défaut convient dans la quasi-totalité des cas.

```bash
python extract_voices.py --tolerance-ms 50
```

## Ce qui est produit

```
output/<nom du replay>/
    Pseudo1.wav
    Pseudo2.wav
    ...
    voices.json
```

Les WAV sont en 48 kHz, mono, 16 bits, et font tous exactement la durée du replay. Ils démarrent à t=0 : il suffit de les déposer sur autant de pistes dans le logiciel de montage, aucun recalage n'est nécessaire.

Les fichiers portent le pseudo du joueur quand il a pu être retrouvé et **vérifié** (serveurs en offline mode, où l'UUID dérive du pseudo). Sinon, le nom du fichier est l'UUID, et `voices.json` signale `"name_verified": false`.

`voices.json` contient, pour chaque locuteur, son UUID, son temps de parole et la liste de tous ses segments de parole en timecodes secondes :

```json
{
  "uuid": "e7922493-7829-3658-9617-0af43ae853c9",
  "name": "LuKaL3",
  "name_verified": true,
  "file": "LuKaL3.wav",
  "speech_seconds": 7671.4,
  "duration_seconds": 10612.65,
  "segments": [[0.05, 3.42], [7.9, 9.15]]
}
```

## Espace disque

Compter **environ 1 Go par locuteur et par heure de replay**. Les silences occupent de la place mais ne sont pas écrits : ils sont laissés vides dans le fichier.

Pour compresser sans perte après coup, avec [ffmpeg](https://ffmpeg.org/) :

```bash
ffmpeg -i "output/mon replay/LuKaL3.wav" -c:a flac "LuKaL3.flac"
```

## Performances

Environ 400 Mo de replay par seconde. Un replay de 3 heures et 4,3 Go, contenant 1,9 million de trames voix, se traite en une dizaine de secondes.

## Problèmes courants

**`aucun replay trouve dans input/`** — le dossier placé dans `input/` doit contenir directement `metadata.json`. Vérifier qu'il n'y a pas un niveau de dossier en trop.

**`aucun paquet voix trouve`** — le replay a été enregistré sans Simple Voice Chat, ou avec l'option `recordVoiceChat` désactivée dans la configuration de Flashback. Dans ce cas les voix ne sont pas dans le fichier et rien ne permet de les récupérer.

**Une piste porte un UUID au lieu d'un pseudo** — le joueur n'apparaissait pas dans les paquets du premier chunk, ou le serveur est en online mode (les UUID n'y dérivent pas du pseudo). L'audio est correct, seul le nom du fichier est concerné.

**`[!] N ticks lus, M attendus`** — le nombre de ticks compté dans un chunk ne correspond pas à `metadata.json`. L'extraction continue, mais la synchronisation des chunks suivants peut dériver. Signe d'un replay tronqué ou corrompu.

## Fonctionnement

Le format du conteneur et la logique de reconstruction de la timeline sont documentés en tête de [extract_voices.py](extract_voices.py).
