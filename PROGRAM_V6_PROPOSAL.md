# Prospect Research Program v6

## Changements majeurs v5 → v6

### DIAGNOSTIC v5: Les corrections v5 ne sont PAS ACTIVES en production

**Preuve:** Les 74 prospects du cycle 58 ont TOUS score=22.5, company=None, headline="• 3rd+ degree connection". Cela prouve que:
1. Le headline gate (v5) NE FILTRE PAS — sinon ces profils n'existeraient pas en DB
2. Le scoring strict (v5) N'EST PAS APPLIQUÉ — sinon score ≠ 22.5 (ce serait ~1.0 pour profils vides)
3. Le deep screening est TOUJOURS CASSÉ — aucune company extraite

**Cause probable:** Les fichiers modifiés (`app/scheduler/jobs.py`, `app/services/scoring_service.py`) ne sont pas commités/déployés. Git status montre des modifications non-staged.

### PRIORITÉ ABSOLUE v6: Déployer v5 + valider que ça fonctionne

Avant TOUT changement de programme, il faut:
1. **Commiter et déployer** les corrections v5 déjà dans le code
2. **Ajouter un health check** au démarrage pour valider le deep screening
3. **Attendre 3 cycles** avec le code v5 actif avant de conclure
4. **Si après 3 cycles** le taux d'approbation humaine est toujours < 10% → appliquer les corrections v6 ci-dessous

---

### CORRECTION v6-1: Health check obligatoire au démarrage du cycle

Avant le Step 1 (generate queries), ajouter:
```python
# Health check: deep-screen a known profile to validate extraction
HEALTH_CHECK_USERNAME = "benoitddlp"  # ou un profil connu, stable
test_profile = await _scraper.get_person_profile(HEALTH_CHECK_USERNAME)
if not test_profile or not test_profile.get("current_company"):
    logger.error("HEALTH CHECK FAILED: deep screening broken (no company extracted for %s)", HEALTH_CHECK_USERNAME)
    LOOP_STATE["last_error"] = "Deep screening broken — health check failed"
    return  # ABORT cycle
logger.info("HEALTH CHECK PASSED: %s @ %s", test_profile.get("full_name"), test_profile.get("current_company"))
```

### CORRECTION v6-2: Minimum score pour deep screening

Ne pas gaspiller des visites de profil sur des prospects qui n'ont aucun signal tech:
```python
# Au lieu de: WHERE experience_json IS NULL ... ORDER BY created_at DESC LIMIT 40
# Utiliser: WHERE experience_json IS NULL ... AND relevance_score >= 8 ORDER BY relevance_score DESC LIMIT 30
```
Un prospect avec headline="CTO at Grab" + location="Singapore" aura un score ~30 même sans deep screen. Un prospect sans headline utile aura score < 5. Ne deep-screen que ceux qui ont un minimum de signal.

### CORRECTION v6-3: Score plafond pour non-deep-screened réduit à 18

Actuellement cap_at_15 ne s'applique que si < 2 champs parmi (company, location, title). Ajouter un cap global:
```python
# Tout prospect sans screened_at → cap à 18 (pas 25)
if not prospect.get("screened_at"):
    score = min(score, 18.0)
    breakdown["gate"] = "not_deep_screened"
```

### CORRECTION v6-4: "IT Director" banni comme query

"IT Director" @ Singapore → 41 prospects, score moyen 22, 0 qualifiés. C'est un titre trop générique qui attire des profils non-tech (IT support, IT admin). Le bannir et le remplacer par "CTO" ou "VP Technology".

### CORRECTION v6-5: Validation des CSS selectors au démarrage

Ajouter un check que les sélecteurs CSS de `get_person_profile()` sont encore valides:
```python
# Après le health check, vérifier que les champs critiques sont extraits
required_fields = ["current_company", "current_title", "experience"]
missing = [f for f in required_fields if not test_profile.get(f)]
if missing:
    logger.error("CSS SELECTOR CHECK: missing fields %s — LinkedIn may have changed DOM", missing)
```

### CORRECTION v6-6: Anti-garbage renforcé dans _score_title()

Le fuzzy matching de SequenceMatcher peut donner des faux positifs même avec threshold 0.6. Ajouter une vérification de longueur minimale:
```python
# Dans _score_title(): rejeter les titres trop courts (< 3 chars) ou trop longs (> 200 chars, likely garbage)
if len(title) < 3 or len(title) > 200:
    return 0.0
```

### CORRECTION v6-7: Forcer la diversité géographique

À chaque cycle, le `generate_search_plan()` DOIT inclure au moins:
- 2 queries Singapore
- 1 query Vietnam ou Indonesia
- 1 query Thailand, Philippines, ou Malaysia

Si Claude ne respecte pas cette contrainte, le code doit l'enforcer post-génération.

### CORRECTION v6-8: Nouvelles startups SEA 2025-2026

Les listes de startups dans v4/v5 datent. Ajouter:

**Singapore (récentes):**
- Hypotenuse AI, Ryde, MiRXES, Prism+, Coda Payments, Pickupp, Homage, Doctor Anywhere
- Series A-B 2025: Horizon Quantum, Versafleet, StaffAny, Spenmo, Volopay

**Vietnam (récentes):**
- Trusting Social, Manabie, Homebase, KiotViet, VNLIFE, Timo, Finhay/DNSE
- Series A-B 2025: Geniebook Vietnam, JobHopin, Dat Bike

**Indonesia (récentes):**
- Amartha, JULO, Bibit, Ajaib, Pintu, Sicepat, Dagangan
- Series A-B 2025: Bukukas, Majoo, Mekari

**Thailand (récentes):**
- Ascend Money, LINE MAN Wongnai, Pomelo Fashion, Opn (ex-Omise), Bitkub
- Series A-B 2025: Finnomena, Jitta, Zort

### CORRECTION v6-9: Tracking du deep screen success rate

Ajouter une métrique: `deep_screen_success_rate = (prospects with company after deep screen) / (total deep screened)`.
Si ce taux < 30% → les CSS selectors sont probablement cassés.

---

## Objectif
Trouver des décideurs tech à Singapour et en Asie du Sud-Est qui pourraient externaliser du développement digital auprès d'Okoone.

## Profil cible
- Titres: CTO, VP Engineering, Head of Engineering, Head of Product, Technical Director, Engineering Manager, Technical Co-founder, Chief Digital Officer, Co-founder (tech), Head of Technology, Product Director, Digital Director, Tech Lead
- Entreprises: Startups en croissance (Series A-C) ou PME tech (50-500 employés)
- Secteurs: fintech, healthtech, edtech, SaaS B2B, e-commerce, proptech, logistics-tech, traveltech, insurtech, agritech
- Localisation: Singapore (priorité 1), Vietnam (priorité 2), Thailand, Indonesia, Philippines, Malaysia, Cambodia

---

## PHASE 0: HEALTH CHECK (NOUVEAU — avant tout cycle)

### 1. Valider le deep screening
- Deep-screen 1 profil connu (username hardcodé)
- Vérifier que `current_company`, `current_title`, `experience` sont non-vides
- Si échec → ABORT le cycle entier. Logger l'erreur.

### 2. Valider la session LinkedIn
- Vérifier `is_session_valid()`
- Si non → tenter `_auto_relogin()`
- Si échec → ABORT (déjà implémenté en v5)

---

## PHASE 1: FILTRAGE À L'EXTRACTION (avant insertion en DB)

### Filtres d'extraction (dans jobs.py à l'insertion)
Rejeter AVANT insertion si:
- `headline` commence par "•" (badge connexion LinkedIn)
- `headline` est vide
- `headline` contient "degree connection"
- `headline` ne contient AUCUN signal tech parmi: cto, cio, cdo, cpo, vp, vice president, director, head of, chief, founder, co-founder, engineer, developer, tech, digital, product, software, data, cloud, platform, ai, machine learning, devops, architect
- **v6: `headline` contient UNIQUEMENT "manager" sans autre signal tech → rejeter** (élimine "Office Manager", "Account Manager", etc.)

### Company extraction à l'insertion
Parser le headline pour extraire la company via séparateurs: " at ", " @ ", " | ", " - ", ", "

### Filtres existants (dans `_sanitize_search_results()`)
- `linkedin_username` commence par "ACoA" (ID interne LinkedIn)
- `full_name` est None/vide, < 2 mots, ou match pattern garbage
- `location` matche un pattern UI LinkedIn

---

## PHASE 2: HARD GATES (dans scoring_service.py)

### Gate 1: Garbage name filter
`is_garbage_name()` → score = 0.0. Détecte:
- Noms < 2 mots
- Patterns: "Provides services", "Status is", "View profile", "subscribers", "reactions", "3rd+", "degree connection", "Join LinkedIn", "LinkedIn Member", "Sign in", "ACoA"

### Gate 2: Garbage headline filter
`is_garbage_headline()` → score = 0.0. Détecte:
- "Provides services", "Sign in to view", "• [digit]"
- "degree connection", "subscribers", "reactions", "amazing journey"
- "behind live", "Join LinkedIn", "not you?", "can introduce you to"

### Gate 3: Données minimum
Score plafonné à 15 si < 2 champs parmi (company, location, titre):
- `has_company`: current_company non vide
- `has_location`: location non vide
- `has_title`: current_title non vide OU headline ne commençant pas par "•"

### Gate 4: Not-deep-screened cap (v6 — NOUVEAU)
Score plafonné à **18** si `screened_at IS NULL`. Sans deep screen, le score n'est pas fiable.

### Gate 5: Anti-compétiteur
Rejeter si headline/company contient:
- Agences dev: "outsourcing", "offshore development", "software house", "dev shop", "nearshore"
- Freelance: "freelance", "independent consultant", "self-employed"
- Consulting IT: "IT consulting", "technology consulting"

### Gate 6: Title length (v6 — NOUVEAU)
Title/headline < 3 caractères ou > 200 caractères → score = 0.0

---

## PHASE 3: SCORING (après les gates)

### Pondérations

| Critère | Poids | Fonction |
|---------|-------|----------|
| title_match | 25 | Fuzzy match contre TARGET_TITLES (seuil > 0.6) |
| company_fit | 20 | 0.0 si vide, 0.5 baseline, bonus tech/startup |
| seniority | 20 | Keywords dans SENIORITY_MAP |
| industry | 15 | Keywords TARGET_INDUSTRIES dans headline/about/experience |
| location | 10 | Singapore=1.0, SEA=0.7, APAC=0.5, West=0.3, vide=0.05 |
| completeness | 5 | Email, about, experience, education, skills, photo |
| activity | 5 | Baseline=0.1, bonus about>100chars, skills≥5, screened_at |

### Baselines v6 (identiques v5)
- company_fit: company vide → **0.0**
- location: vide → **0.05**
- activity: baseline → **0.1**
- title_match: fuzzy ratio < 0.6 → **0.0**

### TARGET_TITLES
```
cto, vp engineering, head of engineering, head of digital, head of product,
chief digital, chief technology, founder, ceo, technical director,
engineering manager, tech lead, product director, digital director,
head of technology, chief product officer, co-founder
```

**v6: Supprimé** "it director" de la cible. Trop générique, attire des profils IT support/admin.

### LOCATION_TIERS
- 1.0: singapore
- 0.7: vietnam, thailand, indonesia, malaysia, philippines, cambodia, myanmar, laos, brunei, ho chi minh, hanoi, bangkok, jakarta, kuala lumpur, manila, phnom penh, da nang, cebu, surabaya, chiang mai, bali, penang
- 0.5: japan, korea, taiwan, hong kong, china, india, australia, tokyo, seoul, sydney, mumbai, bangalore, taipei, shenzhen
- 0.3: united states, usa, uk, germany, france, canada, netherlands, switzerland, europe

### Seuil de qualification: 40

---

## PHASE 4: DEEP SCREENING

### Session validation (v5, maintenu)
Avant de sauvegarder les données d'un deep screen, vérifier:
1. `about_text` ne contient PAS de signaux de page login (≥2 parmi: "Welcome back", "Sign in", "Forgot password", "Join now", "User Agreement")
2. Si login détecté → **arrêter tout le deep screening** immédiatement
3. Si experience a toutes les companies vides → ne pas sauvegarder l'experience garbage

### Pré-filtre deep screening (v6 — NOUVEAU)
Ne deep-screen que les prospects avec:
- `relevance_score >= 8` (au moins 1 signal titre ou location)
- Ordre par `relevance_score DESC` (meilleurs d'abord)
- Limite: **30** profiles par cycle (réduit de 40)

### Extraction company/title
1. D'abord: experience[0].company et experience[0].title
2. Fallback: parser headline "CTO at Company" / "CTO | Company" / "CTO - Company"
3. Si tout échoue → plafonné à 15

### Success rate tracking (v6 — NOUVEAU)
Compter: `deep_screen_with_company / total_deep_screened`. Si < 30% → alerter que les CSS selectors sont probablement cassés.

---

## PHASE 5: STRATÉGIE DE RECHERCHE v6

### RÈGLE D'OR: Maximum 3 mots-clés par query
Confirmé: toutes les queries > 3 mots ont produit 0 qualifiés.

### PAGINATION: Maximum 3 pages par query
Pages 4+ = bruit. 3 pages × 10 résultats = ~30 résultats/query, suffisant.

### PRIORITÉ 0: Queries par entreprise connue en SEA

**Singapore (funded startups, mis à jour 2026):**
- "CTO Carousell", "CTO Xendit", "CTO PropertyGuru", "CTO StashAway"
- "CTO Funding Societies", "CTO Endowus", "CTO PatSnap", "CTO Aspire"
- "VP Engineering ShopBack", "CTO Ninja Van", "CTO Advance Intelligence"
- "CTO Nium", "CTO Carro", "CTO Glints", "CTO Intellect"
- **v6:** "CTO Hypotenuse AI", "CTO Prism+", "CTO Coda Payments", "CTO Doctor Anywhere"
- **v6:** "CTO StaffAny", "CTO Spenmo", "CTO Volopay", "CTO Homage"

**Indonesia:**
- "CTO Traveloka", "CTO Bukalapak", "CTO Akulaku"
- "CTO Kopi Kenangan", "CTO eFishery", "CTO Stockbit"
- **v6:** "CTO Amartha", "CTO JULO", "CTO Bibit", "CTO Ajaib", "CTO Mekari"

**Vietnam:**
- "CTO VNPay", "CTO MoMo", "CTO Tiki", "CTO Sky Mavis"
- "CTO KiotViet", "CTO Base.vn"
- **v6:** "CTO Trusting Social", "CTO Manabie", "CTO Homebase", "CTO VNLIFE"

**Thailand/Philippines/Malaysia:**
- "CTO Bitkub", "CTO Omise", "CTO Flash Express"
- "CTO Mynt", "CTO PayMongo", "CTO Kumu"
- "CTO Carsome", "CTO GoGet", "CTO Aerodyne"
- **v6:** "CTO Ascend Money", "CTO LINE MAN", "CTO Opn", "CTO Finnomena"

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
- "Co-founder" + location:Singapore

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

### PRIORITÉ 3: Queries francophones
- "French CTO" + location:Singapore
- "CTO français" + location:Singapore
- "directeur technique" + location:Singapore
- "French CTO" + location:Vietnam

### QUERIES BANNIES
- Toute query > 4 mots
- **v6: "IT Director" @ toute location** (trop générique, 41 prospects à 22 de score moyen, 0 qualifiés)
- "CTO cofounder B2B SaaS seed funding" → 6 mots, 0 qualifiés
- "VP Technology digital transformation retail" → 5 mots, 0 qualifiés
- "Head of Software Development travel hospitality" → 5 mots, 0 qualifiés
- "engineering manager cloud native Series A B" → 6 mots, 0 qualifiés
- "directeur technique fintech Asie du Sud-Est" → retourne des Européens
- Toute query avec "hiring", "developers", "scaling" dans les keywords
- Toute query déjà utilisée 2+ fois avec 0 qualifiés

### ROTATION PAR CYCLE
1. Au moins 2 localisations différentes
2. Au moins 2 titres différents
3. Ne pas répéter une query identique au cycle précédent
4. Inclure au moins 1 query de PRIORITÉ 0 (company-first) et 1 de PRIORITÉ 1 (titre + location)
5. **v6: Au moins 1 query hors Singapore** (forcer la diversité géographique)

### DIVERSITÉ GÉOGRAPHIQUE FORCÉE (v6 — NOUVEAU)
Distribution minimale par cycle de 5 queries:
- 2 queries Singapore
- 1 query Vietnam OU Indonesia
- 1 query Thailand, Philippines, OU Malaysia
- 1 query libre (company-first recommandé)

---

## PHASE 6: VALIDATION POST-DEEP-SCREEN

### Session health check (v5, maintenu)
Si le deep screen d'un profil retourne about_text contenant des signaux de login → session morte. Arrêter immédiatement.

### Company extraction fallback
1. experience_json[0]["company"] (si non vide)
2. Parser headline: "CTO at Company" / "CTO | Company" / "CTO - Company" / "CTO, Company"
3. Si tout échoue → "company_unknown", plafonné à 15

### Re-scoring
Après enrichissement, re-scorer avec les données complètes. Le score devrait monter significativement si company+location+title sont identifiés.

---

## PHASE 7: MÉTRIQUES DE SUCCÈS

| Métrique | Cible v6 | Actuel | Comment mesurer |
|----------|----------|--------|-----------------|
| Human approval rate | > 15% | ~0% | MÉTRIQUE PRINCIPALE |
| **Deep screen success rate** | > 50% | ~0% | **v6: NOUVEAU — company extraite / total deep-screened** |
| Garbage rate in DB | < 10% | 75% | Headline gate + sanitization |
| Prospects avec company | > 50% | 0% | Deep screen fix + headline extraction |
| Score moyen (non-zero) | > 30 | 22.5 (inflated) | Strict scoring |
| Qualification rate (>40) | > 15% | 0% | Lower baselines + better data |
| Novelty rate | > 30% | 32% | Maintenir |
| Diversity (secteurs) | > 3/cycle | 0 | Query rotation + geo diversity |
| **Health check pass rate** | 100% | N/A | **v6: NOUVEAU — cycles où le health check passe** |

---

## CHANGEMENTS CODE REQUIS (par priorité)

### P0: DÉPLOYER v5 (BLOQUANT)
```bash
git add app/scheduler/jobs.py app/services/scoring_service.py
git commit -m "deploy v5: headline gate, strict scoring, login detection, reduced pagination"
# Redémarrer le service
```

### P1: Health check au démarrage (jobs.py)
Avant Step 1, ajouter un deep-screen test d'un profil connu. Si échec → abort cycle.

### P2: Cap not-deep-screened à 18 (scoring_service.py)
Après le calcul du score, ajouter:
```python
if not prospect.get("screened_at"):
    score = min(score, 18.0)
    breakdown["gate"] = "not_deep_screened"
```

### P3: Pré-filtre deep screening par score (jobs.py)
Changer la query deep screening:
```sql
WHERE (experience_json IS NULL OR experience_json = '' OR experience_json = '[]')
AND relevance_score >= 8
AND linkedin_username IS NOT NULL
ORDER BY relevance_score DESC LIMIT 30
```

### P4: Bannir "IT Director" (autoresearch_service.py)
Ajouter "IT Director" à la liste des queries bannies dans le prompt Claude.

### P5: Deep screen success rate metric (jobs.py)
Après le deep screening, calculer et logger:
```python
ds_with_company = sum(1 for ... if profile.get("current_company"))
ds_success_rate = ds_with_company / max(deep_screened, 1)
logger.info("DEEP SCREEN SUCCESS RATE: %.0f%% (%d/%d)", ds_success_rate*100, ds_with_company, deep_screened)
if ds_success_rate < 0.3 and deep_screened >= 5:
    logger.error("CSS SELECTORS LIKELY BROKEN — deep screen success rate < 30%%")
```

### P6: Diversité géographique forcée (autoresearch_service.py)
Post-process les queries générées par Claude: si toutes les queries sont Singapore, remplacer 2 d'entre elles par Vietnam et Thailand.

### P7: Title length gate (scoring_service.py)
```python
if len(title) < 3 or len(title) > 200:
    return 0.0
```

### P8: Headline "manager" seul = rejet (jobs.py)
Dans le headline gate, si le seul signal tech trouvé est "manager" et qu'aucun autre signal tech n'est présent → rejeter.
```python
if has_tech_signal and headline_lower.count("manager") and not any(
    sig in headline_lower for sig in _TECH_SIGNALS if sig != "manager"
):
    _filtered_at_insertion += 1
    continue
```
