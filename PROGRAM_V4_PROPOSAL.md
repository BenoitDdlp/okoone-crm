# Prospect Research Program v4

## Changements majeurs v3 → v4

### CORRECTION CRITIQUE 1: Hard gates dans le scoring (IMPLÉMENTÉS dans le code, pas juste documentés)
Le scoring v3 donnait des points à des profils sans données (company_fit baseline=0.5, location=0.2).
v4 applique les garbage filters AVANT le scoring et zéro les baselines pour champs vides.

### CORRECTION CRITIQUE 2: Deep screen obligatoire
Aucun prospect ne peut être qualifié (score > 40) sans avoir été deep-screened.
Les search-only prospects sont plafonnés à score=25 max.

### CORRECTION CRITIQUE 3: Queries ultra-courtes (max 3 mots)
Toutes les queries historiques à 5+ mots ont produit 0 qualifiés.
v4 impose max 3 mots-clés + utilisation des filtres LinkedIn natifs.

### CORRECTION CRITIQUE 4: Fix du deep screening
100% des prospects ont current_company=None. Le deep screening est cassé ou pas appelé.
Sans company, le pipeline ne peut pas fonctionner.

### CORRECTION CRITIQUE 5: Company-first search strategy
Chercher des ENTREPRISES connues en SEA d'abord, puis trouver leurs décideurs.
Inverse l'approche v1-v3 qui cherchait des titres dans le vide.

### CORRECTION CRITIQUE 6 (NOUVEAU): Gate immédiat à l'extraction
Ne pas attendre le scoring pour filtrer. Rejeter les profils garbage DÈS l'extraction
dans `_sanitize_search_results()`. Un profil qui ne passe pas Gate 1 ne devrait JAMAIS
entrer en base de données.

---

## Objectif
Trouver des décideurs tech à Singapour et en Asie du Sud-Est qui pourraient externaliser du développement digital auprès d'Okoone.

## Profil cible
- Titres: CTO, VP Engineering, Head of Engineering, Head of Product, IT Director, Technical Director, Engineering Manager, Technical Co-founder, Chief Digital Officer, Co-founder (tech)
- Entreprises: Startups en croissance (Series A-C) ou PME tech (50-500 employés)
- Secteurs: fintech, healthtech, edtech, SaaS B2B, e-commerce, proptech, logistics-tech, traveltech, insurtech, agritech
- Localisation: Singapore (priorité 1), Vietnam (priorité 2), Thailand, Indonesia, Philippines, Malaysia

---

## PHASE 0: FILTRAGE À L'EXTRACTION (NOUVEAU — avant même l'insertion en DB)

### Principe
Un profil qui ne peut pas être un prospect RÉEL ne doit JAMAIS entrer en base.
Cela réduit le bruit pour le scoring, économise les tokens Claude, et évite la pollution des métriques.

### Filtres d'extraction (dans `_sanitize_search_results()` et `search_people()`)
Rejeter AVANT insertion si:
- `linkedin_username` commence par "ACoA" (ID interne LinkedIn, pas un profil)
- `full_name` est None/vide après nettoyage
- `full_name` n'a pas d'espace (= username, pas un nom: "Alainregnier", "Databaker", "Fxlemire")
- `full_name` contient un pattern garbage (regex): `Provides services|Status is|View.*profile|subscribers|reactions|3rd\+|degree connection|Join LinkedIn|LinkedIn Member|• `
- `headline` est UNIQUEMENT "• Xnd/rd/th+ degree connection" sans autre contenu
- `location` matche un pattern UI LinkedIn: `seek to live|currently behind|amazing journey|subscribers|reactions|\d+k `

### Métriques de filtrage
Logger le nombre de profils filtrés par pattern pour identifier les problèmes récurrents.
Cible: > 50% des résultats bruts filtrés = le scraping capte trop de bruit, ajuster les queries.

---

## PHASE 1: HARD GATES (avant tout scoring, après insertion)

### Gate 1: Garbage data filter
Un prospect est IMMÉDIATEMENT rejeté (score = 0, status = gate_rejected) si:
- `full_name` est None/vide
- `full_name` contient: "Provides services", "Status is", "View", "subscribers", "reactions", "3rd+", "degree connection", "• "
- `full_name` est un seul mot sans espace (username LinkedIn comme "Alainregnier", "Databaker")
- `full_name` contient des caractères spéciaux inhabituels (sauf apostrophes, accents, hyphens, espaces)
- `location` contient des mots LinkedIn UI: "seek", "live", "behind", "subscribers", "amazing", "journey", "reactions", "8000k"
- `headline` commence par "Provides services" ou "• 3rd+" ou est identique à "• Xnd/rd/th degree connection"
- `headline` contient "can introduce you to" (carte LinkedIn, pas un vrai profil)
- `headline` est vide ET `location` est vide ET `current_company` est vide (= fantôme)

### Gate 2: Données minimum
Un prospect est rejeté (score = 0) si les 3 conditions sont TOUTES vraies:
- `current_company` est null/vide
- `location` est vide ou non-identifiable comme SEA/APAC
- `headline` ne contient aucun mot de TARGET_TITLES

Autrement dit: au moins 1 signal fort d'identification est requis.

### Gate 3: Plafond search-only
Un prospect qui n'a PAS été deep-screened (screened_at = null) est plafonné à score=25.
Raison: sans experience, skills, about — le score ne peut pas être fiable.

### Gate 4 (NOUVEAU): Anti-compétiteur
Un prospect est rejeté si `current_company` ou `headline` contient un pattern de concurrent:
- Agences dev: "outsourcing", "offshore development", "software house", "dev shop", "nearshore"
- Freelance: "freelance", "independent consultant", "self-employed", "solopreneur"
- Consulting IT: "IT consulting", "technology consulting" (sauf si titre = CTO/VP = client potentiel)

---

## PHASE 2: SCORING (après les gates)

### Changements par rapport à v3

| Critère | v3 | v4 | Raison |
|---------|----|----|--------|
| company_fit baseline (company vide) | 0.5 | **0.0** | Pas de company = pas de fit |
| location (unknown) | 0.2 | **0.0** | Location inconnue = inutile |
| location (no match) | 0.1 | **0.05** | Encore moins de points gratuits |
| activity baseline | 0.3 | **0.1** | Moins de points gratuits |
| seuil qualification | 50 | **40** | Ajusté car baselines à zéro réduisent les scores légitimes |

### Pondérations ajustées

| Critère | Poids v3 | Poids v4 | Raison |
|---------|----------|----------|--------|
| title_match | 25 | **30** | Le titre est le signal #1 |
| company_fit | 20 | **25** | Company identifiée = prospect réel |
| seniority | 20 | **15** | Redondant avec title_match |
| industry | 15 | **15** | Inchangé |
| location | 10 | **10** | Inchangé |
| completeness | 5 | **3** | Récompense les données, pas la qualité |
| activity | 5 | **2** | Signal trop faible |

### TARGET_TITLES élargi
Ajouter: "engineering manager", "tech lead", "product director", "digital director", "head of technology", "chief product officer", "co-founder"

### Villes SEA manquantes dans LOCATION_TIERS
Ajouter au tier 0.7: "da nang", "cebu", "surabaya", "chiang mai", "phuket", "bali", "penang", "johor", "batam", "phnom penh", "vientiane"
Ajouter au tier 0.5 (APAC élargi): "taipei", "hong kong", "shenzhen" (hubs tech proches de SEA)

### _score_title() — Réduire le fuzzy matching
Seulement compter le fuzzy match si `SequenceMatcher.ratio() > 0.6`. Actuellement tout ratio est compté, ce qui donne des faux positifs.

### Signaux positifs (bonus post-scoring, max +15 points)
- [+10] Recrute des développeurs (offres ouvertes visibles dans experience/about)
- [+8] Francophone avec rôle tech en SEA (canal privilégié Okoone)
- [+5] Petite équipe tech (< 20 devs) mentionnée dans about/experience
- [+5] Entreprise a levé des fonds récemment (mentionné dans headline/about)
- [+3] Background agence/consulting passé (comprend le modèle)
- [+3] Connexions mutuelles avec l'équipe Okoone
- [+2] Headline contient "building", "scaling", "growing"

### Signaux négatifs (éliminatoires ou malus)
- [ÉLIMINATOIRE → score=0] Freelancer/service provider (headline "Provides services", "Freelance", "Independent consultant")
- [ÉLIMINATOIRE → score=0] Pure consulting/outsourcing/dev agency (concurrent direct d'Okoone)
- [ÉLIMINATOIRE → score=0] Profil LinkedIn marketplace card
- [-20] Entreprise > 1000 employés avec équipe tech > 50 personnes
- [-15] Location hors Asie-Pacifique (quand identifiable)
- [-10] Profil marketing/sales pur sans composante tech
- [-5] Entreprise pre-revenue sans financement visible

---

## PHASE 3: STRATÉGIE DE RECHERCHE v4

### RÈGLE D'OR: Maximum 3 mots-clés par query
Toutes les queries à 5+ mots ont produit 0 qualifiés. LinkedIn interprète mal les longues queries.

### PRIORITÉ 0 (NOUVEAU): Queries par entreprise connue en SEA
Les startups funded en SEA sont le segment idéal. Chercher directement:

**Singapore (Series A-C):**
- "CTO Carousell", "CTO Xendit", "CTO PropertyGuru", "CTO StashAway"
- "CTO Funding Societies", "CTO Endowus", "CTO PatSnap", "CTO Aspire"
- "VP Engineering ShopBack", "CTO Ninja Van", "CTO Advance Intelligence"
- "CTO Nium", "CTO Carro", "CTO Glints", "CTO Intellect"
- "CTO Moglix", "CTO Zenyum", "CTO Neuron Mobility"

**Indonesia:**
- "CTO GoTo", "CTO Traveloka", "CTO Bukalapak", "CTO Akulaku"
- "CTO Kopi Kenangan", "CTO Ula", "CTO Xendit Indonesia"
- "CTO eFishery", "CTO SiCepat", "CTO Stockbit"

**Vietnam:**
- "CTO VNPay", "CTO MoMo", "CTO Tiki", "CTO Sky Mavis"
- "CTO KiotViet", "CTO Base.vn", "CTO Got It"

**Thailand/Philippines/Malaysia:**
- "CTO Bitkub", "CTO Omise", "CTO Flash Express" (Thailand)
- "CTO Mynt", "CTO PayMongo", "CTO Kumu" (Philippines)
- "CTO Carsome", "CTO GoGet", "CTO Aerodyne" (Malaysia)

Portfolios à explorer: Y Combinator SEA, Antler Singapore, Iterative.vc, Golden Gate Ventures, Jungle Ventures, East Ventures, Wavemaker Partners, Insignia Ventures

### PRIORITÉ 1: Queries minimales (2-3 mots + location filter)
- "CTO" + location:Singapore
- "CTO" + location:Vietnam
- "CTO" + location:"Ho Chi Minh City"
- "VP Engineering" + location:Singapore
- "Head Engineering" + location:Singapore
- "CTO" + location:Bangkok
- "CTO" + location:Jakarta
- "CTO" + location:"Kuala Lumpur"
- "CTO" + location:Manila
- "Engineering Manager" + location:Singapore
- "Technical Director" + location:Singapore
- "Head of Product" + location:Singapore
- "Co-founder" + location:Singapore (NOUVEAU — décideurs tech en startup)

### PRIORITÉ 2: Queries sectorielles courtes (rotation par cycle)
Cycle N: Singapore + fintech/healthtech
- "CTO fintech" + location:Singapore
- "CTO healthtech" + location:Singapore

Cycle N+1: Vietnam + SaaS/edtech
- "CTO SaaS" + location:Vietnam
- "CTO edtech" + location:"Ho Chi Minh City"

Cycle N+2: Thailand + e-commerce/logistics
- "CTO ecommerce" + location:Bangkok
- "CTO logistics" + location:Bangkok

Cycle N+3: Indonesia + fintech/proptech
- "CTO fintech" + location:Jakarta
- "CTO proptech" + location:Jakarta

Cycle N+4: Philippines/Malaysia + SaaS
- "CTO SaaS" + location:Manila
- "CTO" + location:"Kuala Lumpur"

Cycle N+5: Singapore + insurtech/traveltech (NOUVEAU)
- "CTO insurtech" + location:Singapore
- "CTO traveltech" + location:Singapore

### PRIORITÉ 3: Queries francophones (CORRIGÉES)
- "French CTO" + location:Singapore
- "CTO français" + location:Singapore
- "directeur technique" + location:Singapore (PAS "Asie du Sud-Est")
- "French CTO" + location:Vietnam (NOUVEAU — communauté française à HCMC/Hanoi)
- NE JAMAIS chercher: "directeur technique Asie du Sud-Est" (retourne 100% européens)

### PRIORITÉ 4 (NOUVEAU): Queries par signal d'achat
- "hiring engineers" + location:Singapore (entreprises qui recrutent = besoin de capacité)
- "scaling team" + location:Singapore
- "Series A" + location:Singapore (entreprises récemment funded)

### QUERIES BANNIES
- Toute query > 4 mots
- "Chief Technology Officer hiring developers Singapore" → 0 résultats
- "CTO blockchain fintech Series A" → 0 résultats
- "CTO agritech food tech startup scaling" → 0 résultats
- "CTO cofounder B2B SaaS seed funding" → 6 mots, 0 qualifiés
- "directeur technique fintech Asie du Sud-Est" → retourne des Européens
- "VP Technology digital transformation retail" → 5 mots, 0 qualifiés
- "Head of Software Development travel hospitality" → 5 mots, 0 qualifiés
- "engineering manager cloud native Series A B" → 6 mots, 0 qualifiés
- "Engineering Manager SaaS Singapore" → 0 résultats (utiliser location filter natif)
- "Director of Technology Singapore" → 0 résultats
- "Director of Engineering Singapore" → 0 résultats
- "Chief Product Officer technology platform Singapore" → 0 résultats
- "CTO traveltech Jakarta" → 0 résultats
- "CTO mobile app startup Series A" → 0 résultats
- Toute query avec "hiring", "developers", "scaling" dans les keywords → pollue les résultats
- Toute query déjà utilisée 2+ fois avec 0 qualifiés

### ROTATION PAR CYCLE
Chaque cycle doit:
1. Utiliser au moins 2 localisations différentes
2. Utiliser au moins 2 titres différents
3. Ne pas répéter une query identique au cycle précédent
4. Inclure au moins 1 query de PRIORITÉ 0 (company-first) et 1 de PRIORITÉ 1 (titre + location)
5. NOUVEAU: Tracker les queries par cycle_id pour éviter toute répétition sur 5 cycles glissants

---

## PHASE 4: VALIDATION POST-DEEP-SCREEN

Après le deep screening, vérifier:
1. `current_company` est rempli — si non, extraire du 1er item de experience_json
2. `current_title` est rempli — si non, extraire du 1er item de experience_json ou headline
3. `location` est une vraie ville/pays identifiable
4. `experience_json` contient au moins 1 entrée valide
5. Si tout est rempli → re-scorer avec les nouvelles données (le score monte)
6. Si deep screen n'a rien extrait → garder plafond à 25, marquer "insufficient_data"
7. NOUVEAU: Si `current_company` identifiée comme concurrent (agence dev/outsourcing) → score = 0

### Company extraction fallback chain (NOUVEAU)
Si `current_company` est vide après deep screen:
1. Essayer experience_json[0]["company"]
2. Essayer de parser le headline: "CTO at CompanyName" → "CompanyName"
3. Essayer de parser le headline: "CTO | CompanyName" → "CompanyName"
4. Essayer de parser le headline: "CTO, CompanyName" → "CompanyName"
5. Si tout échoue → marquer "company_unknown", plafonner à 25

---

## PHASE 5: MÉTRIQUES DE SUCCÈS

| Métrique | Cible v4 | Actuel | Commentaire |
|----------|----------|--------|-------------|
| Human approval rate | > 20% | 1% | MÉTRIQUE PRINCIPALE |
| Prospects avec company identifiée | > 70% | ~0% | Gate 2 + deep screen fix |
| Prospects avec location SEA valide | > 60% | ~0% | Gate 1 + filtres |
| Taux de qualification (score > 40) | > 20% | 0% | Scoring + gates |
| Novelty rate | > 40% | 6% | Nouvelles queries + rotation |
| Diversity (secteurs) | > 3 par cycle | 1 | Rotation queries |
| Garbage rate (artefacts passant les gates) | < 5% | ~80% | Hard gates + Phase 0 |
| Gate rejection rate | > 50% | N/A | Le gate doit filtrer agressivement |
| NOUVEAU: Deep screen success rate | > 60% | ~0% | Mesurer company extraction |
| NOUVEAU: Query yield (résultats/query) | > 3 | ~1 | Queries plus efficaces |

---

## CHANGEMENTS CODE REQUIS (par priorité)

### P0: Investiguer et fixer le deep screening
Le fait que 100% des prospects aient `current_company = None` est le bug #1.
Vérifier:
- La session LinkedIn est-elle active? (session_manager.check_health())
- `get_person_profile()` est-il appelé? (ajouter des logs)
- L'extraction de company échoue-t-elle silencieusement?
- Les sélecteurs CSS du profil sont-ils à jour? (LinkedIn change souvent ses classes)
- Le JS d'extraction retourne-t-il des données? (logger le résultat brut)
- Si company est dans experience_json mais pas dans current_company, le mapper
- NOUVEAU: Ajouter un test de santé au démarrage — deep-screen 1 profil connu et vérifier que les champs sont extraits

### P1: Filtrage à l'extraction (Phase 0)
Implémenter les filtres dans `_sanitize_search_results()` pour ne JAMAIS insérer de profils garbage en DB.
Cela évite de gaspiller des tokens de deep screen et de scoring sur des fantômes.

### P2: scoring_service.py — Ajouter hard gates
```python
# Au début de score_prospect():
passes, reason = self._hard_gate(prospect)
if not passes:
    return 0.0, {"rejected": reason}

# Après le calcul du score:
if not prospect.get("screened_at"):
    total = min(total, 25.0)
```

### P3: scoring_service.py — Zéro baselines
```python
# _score_company: company vide → 0.0
score = 0.0 if not company else 0.5

# _score_location: location vide → 0.0
if not location:
    return 0.0

# _score_activity: baseline réduit
score = 0.1  # était 0.3
```

### P4: scoring_service.py — Poids et seuils
- Mettre à jour les poids par défaut dans la DB
- Baisser le seuil de qualification de 50 à 40

### P5: autoresearch_service.py — Enforce max 3 mots par query
```python
for query in queries:
    words = query["keywords"].split()
    if len(words) > 4:
        logger.warning("Query trop longue, troncature: %s", query["keywords"])
        query["keywords"] = " ".join(words[:3])
```

### P6: linkedin.py — Renforcer _sanitize_search_results
Ajouter la détection de:
- Single-word usernames (pas d'espace dans le nom)
- Locations contenant "seek", "behind", "reactions", "subscribers"
- Headlines "• 3rd+" patterns
- Noms contenant "can introduce you to"

### P7: prospect_service.py — Company extraction fallback
```python
if not prospect.get("current_company") and prospect.get("experience_json"):
    exp = json.loads(prospect["experience_json"])
    if exp and isinstance(exp, list) and exp[0].get("company"):
        await self.repo.update(prospect_id, {"current_company": exp[0]["company"]})
# Fallback 2: parse headline "CTO at Company" / "CTO | Company" / "CTO, Company"
if not prospect.get("current_company") and prospect.get("headline"):
    for sep in [" at ", " @ ", " | ", ", "]:
        if sep in prospect["headline"]:
            company = prospect["headline"].split(sep, 1)[1].strip()
            if company and len(company) > 1:
                await self.repo.update(prospect_id, {"current_company": company})
                break
```

### P8 (NOUVEAU): Logging diagnostique
Ajouter des compteurs par cycle:
- `raw_results`: nombre brut de résultats extraits par le scraper
- `filtered_at_extraction`: nombre filtrés en Phase 0
- `gate_rejected`: nombre rejetés par les hard gates
- `scored`: nombre effectivement scorés
- `qualified`: nombre au-dessus du seuil
- `deep_screened`: nombre deep-screenés avec succès (company extraite)
- `deep_screen_failed`: nombre deep-screenés sans company extraite

Cela permet de diagnostiquer OÙ le pipeline perd les prospects.

---

## PERFORMANCE DES QUERIES (classées par score moyen décroissant)
- "VP Technology digital transformation retail" @ Thailand → score_moyen=39, qualifies=0/2, meilleur=40, utilise 1x → BANNIR (5 mots)
- "CTO cofounder B2B SaaS seed funding" @ Vietnam → score_moyen=37, qualifies=0/3, meilleur=39, utilise 1x → BANNIR (6 mots)
- "Head of Software Development travel hospitality" @ Southeast Asia → score_moyen=30, qualifies=0/1, meilleur=30, utilise 1x → BANNIR (5 mots)
- "Head of Engineering edtech Vietnam" @ Vietnam → score_moyen=30, qualifies=0/3, meilleur=41, utilise 1x → SIMPLIFIER → "Head Engineering" + location:Vietnam
- "technical director logistics Bangkok" @ Thailand → score_moyen=24, qualifies=0/2, meilleur=24, utilise 1x → GARDER (3 mots)
- "CTO proptech Singapore" @ Singapore → score_moyen=24, qualifies=0/1, meilleur=24, utilise 1x → GARDER (2 mots)
- "engineering manager cloud native Series A B" @ Singapore → score_moyen=24, qualifies=0/1, meilleur=24, utilise 1x → BANNIR (6 mots)
- "directeur technique fintech Asie du Sud-Est" → BANNIR (retourne des Européens)
- Toutes les autres: score_moyen=0, 0 résultats → BANNIR

## Résultats des 7 derniers jours (reviews humaines)
Total prospects: 232 | Approuvés: 1 | Rejetés: 7
Taux de qualification humain: 0%

## METRIQUES DES 5 DERNIERS CYCLES (du plus récent au plus ancien)
Cycle 58 (v3): approval=6.2%, found=74, qualified=74, avg_score=22.5, diversity=0
Cycle 57 (v3): approval=8.3%, found=6, qualified=6, avg_score=22.5, diversity=0
Cycle 56 (v3): approval=8.3%, found=71, qualified=71, avg_score=24.0, diversity=6
Cycle 36 (v3): approval=10%, found=6, qualified=6, avg_score=27
Cycle 24 (v1): approval=10%, found=11, qualified=34, avg_score=30.6

## DIAGNOSTIC CYCLE 58 (2026-03-27)
**100% des 74 prospects ont score=22.5, company=None, aucun signal identifiable.**
Les 50 derniers prospects incluent des profils manifestement hors-cible:
"King Game Winner", "camp tech", "liputan informasi", "phay phoum", "moeun veasna"
→ Le pipeline ne filtre PAS et ne deep-screen PAS. Le scoring est inutile sans données.

### Causes racines confirmées par lecture du code:
1. `scoring_service.py:_score_company()` donne baseline=0.5 quand company="" → FIXÉ en P3
2. `scoring_service.py:_score_location()` donne 0.2 quand location="" → FIXÉ en P3
3. `scoring_service.py:_score_activity()` donne baseline=0.3 → FIXÉ en P3
4. `jobs.py:246-314` deep screen limité à 20/cycle et saute si session invalide → P0
5. `_sanitize_search_results()` ne vérifie pas que headline contient un titre tech → P6 renforcé
6. Pagination agressive (10 pages) dilue la qualité → Limiter à 3 pages max

### P9 (NOUVEAU): Headline gate à l'insertion
Dans `jobs.py:218-236`, AVANT d'insérer en DB, vérifier que le headline contient au moins un
mot-clé de TARGET_TITLES ou un mot tech générique (engineer, developer, tech, digital, product, IT).
Si le headline est vide ou ne contient aucun signal tech → ne pas insérer.
Cela élimine 80%+ des prospects garbage sans headline pertinent.

### P10 (NOUVEAU): Limiter pagination à 3 pages
Dans `jobs.py:181`, changer `range(2, 11)` → `range(2, 4)`.
Les pages 4+ retournent des résultats de plus en plus hors-cible.

### P11 (NOUVEAU): Company extraction depuis headline (pre-deep-screen)
Dès l'insertion, parser le headline pour extraire la company:
- "CTO at Grab" → company = "Grab"
- "VP Engineering | ShopBack" → company = "ShopBack"
- "Head of Digital, PropertyGuru" → company = "PropertyGuru"
Cela donne un signal company_fit AVANT le deep screen.

### ORDRE D'IMPLÉMENTATION RÉVISÉ
1. **P0**: Fix deep screening (investiguer pourquoi company=None pour tous)
2. **P3**: Zéro baselines dans scoring (arrêter de donner 22.5 à des profils vides)
3. **P9**: Headline gate à l'insertion (filtrer les profils sans signal tech)
4. **P6+P1**: Renforcer sanitize + Phase 0 extraction filters
5. **P11**: Company extraction depuis headline
6. **P10**: Limiter pagination à 3 pages
7. **P2**: Hard gates dans scoring
8. **P7**: Company extraction fallback post-deep-screen
9. **P5**: Max 3 mots par query
10. **P8**: Logging diagnostique
