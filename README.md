# PRESFOR — Contrôle des contrats (v3) + contrôle des pièces d'identité (v2)

La plateforme reprend **tout le fonctionnement de la première plateforme** (campagnes, règles de contrôle, fiches, exports
RAS / REAMRQUE / Global, dossier complet avec pièces) et remplace sa lecture OCR navigateur par le moteur d'identité v2
(extraction JSON forcée Claude vision | Tesseract local, comparaison rapidfuzz).

| Page | Rôle |
|---|---|
| `/` | **Contrôle des contrats** : campagnes, tableau (20/50/100 par page, filtres RAS/REAMRQUE), fiche éditable, visionneuse de pièces, exports |
| `/identite` | Contrôle d'identité **par lot** (CSV + scans → verdicts, Excel) — outil v2 historique |
| `/docs` | API (OpenAPI) |

## Parcours (page `/`)
1. **Importer** : CSV DIGIFOR (`### TABLE:`), ZIP (CSV, scans, `Inventaire_pieces.csv`, `campagne.json`/rapport JSON), dossier de scans, images/PDF.
   Les imports se cumulent dans la campagne ; envoi par lots (60 fichiers / 80 Mo).
2. Le tableau applique immédiatement les **règles** (`app/rules.json`, port fidèle de la v1) : type de contrat reconnu, superficie,
   durée (unité ans/mois/jours/semaines, > 99 ans), dates début/fin (garantie), numéro de demande, pourcentages de partage (= 100 %),
   taux de récolte, signatures, nom ≠ signataire, champs obligatoires inexploitables, identité des acteurs.
3. **Contrôler les pièces** (moteur au choix) : lit chaque scan (cache SHA-256), rattache les pièces aux acteurs (références du CSV,
   nom de fichier, Inventaire, contenu lu), compare Nom / N° de pièce → pastille *Concordant / Écart détecté / À contrôler*
   et observations « Identité ». Une lecture incertaine ne produit jamais d'écart (→ « non vérifiable »).
4. **Fiche** : champs du contrat et des acteurs modifiables (réévaluation instantanée côté serveur), pièces rattachées, visionneuse
   (pivoter / zoom / ouvrir), JSON de lecture, observation complémentaire, levée d'une observation d'import après vérification,
   **Importer une pièce pour cet acteur**, **Enregistrer**.
5. **Campagnes sauvegardées** : dernier chargement / dernière sauvegarde, ouverture, suppression définitive (pièces comprises).
6. **Exports** : *Excel des remarques* (13 colonnes : Numéro de contrat … Superficie, Autre Observations ; un classeur par fichier source
   + `Global/` si plusieurs sources, nom `Rque_contrat_<source>.xlsx`) ; *Dossier complet avec pièces* (`Global/`, `RAS/`, `REAMRQUE/`,
   `Global/Inputs/`, `campagne.json`, `Controle_pieces.xlsx`, `OCR/resultats.json`, `PIECE SCANNEE/<contrat>/…`, `Inventaire_pieces.csv`, `LEGENDE.txt`).

Stockage : `DATA_DIR` (défaut `./data`) — `campaigns/<id>/campaign.json`, `scans/`, `ocr.json`. **En production, monter un volume
persistant sur `/data`** (sans disque, les campagnes disparaissent au redémarrage/redéploiement).

# PRESFOR v2 — Contrôle des pièces d'identité

Vérifie que **Nom, Prénoms et N° de pièce** saisis par les agents (CSV) correspondent à la pièce scannée
(CNI, passeport, permis, carte CMU, extrait de naissance…).

```
CSV agents ─┐                                   ┌─ extraction JSON forcée (vision Claude | Tesseract local)
            ├─► rattachement scan ↔ ligne ◄─────┤   cache disque par empreinte SHA-256 (reprise gratuite)
Scans/PDF ──┘            │                       └─ MRZ validée par chiffres de contrôle (passeports)
                         ▼
        comparaison rapidfuzz  ─►  verdict par ligne  ─►  interface web · Excel · JSON
```

## 1. Installation

Python 3.10+ requis.

```bash
cd presfor_controle
python3 -m venv .venv && source .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                       # puis renseigner ANTHROPIC_API_KEY
```

Moteur local de repli (optionnel, sans clé API) : installer Tesseract **avec le français**.
- Ubuntu/Debian : `sudo apt install tesseract-ocr tesseract-ocr-fra`
- macOS : `brew install tesseract tesseract-lang`
- Windows : installateur UB-Mannheim, cocher « French », ajouter Tesseract au `PATH`.

## 2. Lancer

```bash
./run.sh                    # Windows : run.bat      (équivaut à : uvicorn app.server:app --port 8000)
```
Ouvrir **http://127.0.0.1:8000** :
1. Déposer le CSV (CSV simple `; , | tab` **ou** export multi-tables DIGIFOR ; les colonnes sont devinées, corrigeables).
2. Déposer les scans (images, PDF, ou un ZIP).
3. Choisir le moteur, lancer. Cliquer une ligne pour voir CSV / pièce côte à côte, les scans et le JSON extrait.
4. Exporter en Excel (résultats, scans non rattachés, synthèse, JSON brut) ou JSON.

Ligne de commande (même moteur, utile pour les gros lots) :
```bash
python -m app.cli control --csv export.csv --scans ./ScanSignataires --out rapport.xlsx
python -m app.cli extract carte.jpg                       # JSON d'une seule pièce
python -m app.cli control ... --map numero=idNumber full=name --tables owners beneficiaries
```
API (intégration à votre plateforme) : `POST /api/extract` (fichier → JSON), `POST /api/jobs`, `GET /api/jobs/{id}`,
documentation interactive sur `/docs`.
```bash
curl -F file=@carte.jpg http://127.0.0.1:8000/api/extract
```

## 3. JSON d'extraction (forcé)

Avec Claude, le format n'est pas « demandé » mais **imposé** : l'appel utilise `tool_choice` sur l'outil
`enregistrer_identite` dont le schéma JSON fixe les champs ; la réponse est ensuite revalidée (pydantic) et
nettoyée. Réponse invalide → une nouvelle tentative, puis erreur explicite (jamais de valeur inventée).

```json
{ "lisible": true, "type_piece": "CNI", "face": "RECTO",
  "nom": "GBRA", "prenoms": "LIBY NESTOR", "numero_piece": "CI007461947", "date_naissance": "30/01/1975",
  "confiance": {"nom": 0.98, "prenoms": 0.97, "numero_piece": 0.99}, "remarques": "" }
```
Un **verso** (père/mère/domicile) renvoie `face: "VERSO"` et des champs `null` : il n'est jamais pris pour un
titulaire. Les champs illisibles sont `null`, jamais devinés.

## 4. Règles de comparaison

| Champ | Résultat | Signification |
|---|---|---|
| **Nom** | `OK` | identique après suppression des accents, casse, ponctuation, **ordre** nom/prénoms indifférent |
| | `TOLERE` | petite variation (score ≥ `NAME_TOLERATE`=90) : accepté, tracé |
| | `PROCHE` | 80 ≤ score < 90, ou nom/prénoms inversés dans le CSV → à vérifier |
| | `PARTIEL` | un prénom manquant ou en trop (le mot concerné est affiché) → à vérifier |
| | `ECART` | différent |
| **N° pièce** | `OK` | identique après normalisation (espaces, tirets, casse) |
| | `PROCHE` | confusion O/0, I/1, S/5… ou 1 caractère d'écart (2 si ≥ 10 car.) → **jamais accepté en silence** |
| | `ECART` | numéros différents |
| les deux | `NON_LU` / `MANQUANT_CSV` / `INCERTAIN` | pièce illisible / champ CSV vide / lecture < `MIN_CONFIDENCE` |

**Pourquoi le numéro est strict** : une faute de frappe d'un caractère sur un numéro est précisément l'erreur
que le contrôle qualité doit détecter ; le flou sert ici à *qualifier* l'écart (frappe, confusion OCR), pas à l'excuser.

Verdict par ligne : `CONFORME`, `CONFORME_TOLERE` (nom toléré, numéro exact), `A_VERIFIER`, `NON_CONFORME`
(écart confirmé sur une lecture fiable, ou champ CSV vide alors que la pièce le porte), `PIECE_MANQUANTE`,
`PIECE_INUTILISABLE` (verso seul, illisible, erreur). `PIECE_MANQUANTE` signifie **aucun scan rattaché à la ligne**
(affiché « Aucun scan rattaché ») : ce n'est pas la preuve que la pièce n'existe pas dans le dossier. **Une lecture incertaine ne donne jamais un `NON_CONFORME`**
(→ `A_VERIFIER`). Seuils réglables dans `.env`.

**Rattachement scan ↔ ligne** : par colonne « fichier » si elle existe ; sinon par numéro identique, ou par
similarité du nom (nom du fichier et nom lu sur la pièce) en priorité dans le même contrat (code `CT-…` du fichier).
Une pièce d'un autre contrat n'est utilisée que si l'identité lue est quasi identique (≥ 90), et signalée.
Le nom de fichier ne sert **qu'à retrouver** la pièce, jamais de valeur de référence. Recto/verso : seuls les champs
lus sur une face portant l'identité sont comparés ; des numéros différents entre scans d'une même ligne → `A_VERIFIER`.
Les scans qu'aucune ligne ne revendique sont listés à part.

## 5. Choix du moteur & limites

- **Vision Claude** (recommandé) : lit les mises en page variées (libellé au-dessus/à côté de la valeur, anciennes et
  nouvelles CNI, photos de travers) et déclare une confiance par champ. Modèle réglable (`ANTHROPIC_MODEL`).
- **Tesseract local** : gratuit, hors-ligne, mais nettement moins robuste sur les photos de mauvaise qualité ;
  ses lectures ont une confiance plafonnée, donc un doute donne `A_VERIFIER`, pas un verdict ferme.
- **Confidentialité** : avec Claude, les images de pièces d'identité sont envoyées à l'API Anthropic. Vérifiez que
  c'est compatible avec vos obligations de protection des données personnelles (information des personnes, base légale,
  contrat de traitement) ; sinon utilisez le moteur local. L'application écoute sur `127.0.0.1` uniquement ; ne
  l'exposez pas sur Internet sans authentification. Les scans déposés sont conservés 24 h dans `data/jobs/`
  (puis supprimés au prochain lot) ; le cache `data/cache/` ne contient que le JSON extrait.
- Un même scan n'est jamais relu deux fois (cache par SHA-256 + moteur) : relancer un lot interrompu reprend où il
  s'est arrêté ; les erreurs ne sont pas mises en cache.
- Seule la première page des PDF (4 pages max) est exploitée ; pas de redressement de perspective.

## Hébergement en ligne (Docker)

```bash
docker build -t presfor .
docker run -p 8000:8000 -v presfor-data:/data \
  -e APP_USER=admin -e APP_PASSWORD='mot-de-passe-long' -e ANTHROPIC_API_KEY=sk-ant-... presfor
```
Équivalent sur Render / Railway / Fly.io / un VPS : déployer ce dossier (Dockerfile détecté), définir les variables
`APP_PASSWORD` (**obligatoire**, sinon l'application est ouverte à tous), `APP_USER`, `ANTHROPIC_API_KEY`, monter un
volume sur `/data`, activer HTTPS (fourni par ces plateformes). Sonde de santé : `/healthz`.
Préférer un hébergeur dont la région et le contrat de traitement conviennent à des pièces d'identité.

### Render (blueprint `render.yaml` inclus)
1. Pousser ce dossier dans un dépôt Git **privé** (GitHub/GitLab).
2. Render → *New → Blueprint* → choisir le dépôt → renseigner `ANTHROPIC_API_KEY`.
3. Récupérer `APP_PASSWORD` (généré) dans *Environment*, ouvrir l'URL `https://presfor-controle.onrender.com`.

## 6. Mesures réelles (jeu DATA : 225 scans, 253 lignes owners+beneficiaries)

Moteur **Tesseract local** uniquement (aucune clé API disponible lors du développement), serveur à 2 cœurs, ~25 min :
- 225 scans traités, 0 erreur ; 102 scans portent au moins un champ lu, 34 les trois champs (le reste : versos, fonds complexes) ;
- lignes : 5 `CONFORME` + 1 `CONFORME_TOLERE` (nom ET numéro identiques au CSV), 33 `A_VERIFIER`, 3 `PIECE_INUTILISABLE`,
  **0 `NON_CONFORME`** (voulu : le moteur local ne confirme jamais un écart), 211 sans scan rattaché ;
- 86 scans non rattachés : 44 sont le même témoin/signataire (« Gnonka Guehi Boniface Mathurin »), d'autres sont des
  personnes absentes des tables `owners`/`beneficiaries`, 10 portent le nom d'une ligne d'un **autre contrat** (non rattachés
  tant que le nom n'est pas LU sur la pièce : proposés comme « ligne la plus proche » dans l'onglet *Scans non rattachés*).
- **Le chemin Claude n'a pas pu être exécuté sur de vraies images ici** (pas de clé). Il est testé avec un client factice
  (requête, JSON forcé, validation, nouvelle tentative). Mesurez-le sur un échantillon de vos scans avant de généraliser :
  `python -m app.cli control --csv … --scans <dossier de 30 scans> --backend claude`.

## 7. Tests

```bash
pip install -r requirements-dev.txt && pytest -q
```
Couvre normalisation, comparaison (OK/tolérance/partiel/inversion/confusions/incertain), MRZ (spécimens OACI),
chemin Claude avec client factice (JSON forcé, validation, retry), CSV DIGIFOR, pipeline, cache et API
(dont zip-slip).
