# Prospect Research Program v7

## Changements majeurs v6 → v7

### DIAGNOSTIC: Le pipeline est CASSÉ, pas sous-performant

**Preuves factuelles:**
- human_approval_rate en baisse: 10% → 10% → 8.3% → 8.3% → 6.2%
- qualification_rate = 0% sur 5 cycles consécutifs
- avg_score convergé à 22.5 (tous les profils identiques = garbage)
- diversity_score = 0 (aucune company extraite)
- 74 prospects avec headline "• 3rd+3rd+ degree connection" → le headline gate n'est PAS actif

**Cause racine:** Le code v5 (headline gate, strict baselines, login detection) est modifié dans les fichiers mais **non déployé** (non commité, service non redémarré).

### CORRECTIONS v7

#### C1: Health check au démarrage du cycle (IMPLEMENTÉ dans jobs.py)
- Deep-screen un profil connu (`benoitddlp`) avant le cycle
- Vérifie que `current_company` et `current_title` sont extraits
- Si échec → ABORT le cycle entier
- Détecte les CSS selectors cassés AVANT de gaspiller des visites

#### C2: Not-deep-screened cap à 18 (IMPLEMENTÉ dans scoring_service.py)
- Tout prospect sans `screened_at` → score plafonné à 18
- Empêche les profils non enrichis de polluer le ranking

#### C3: Pre-filtre deep screening par score (IMPLEMENTÉ dans jobs.py)
- Ne deep-screen que les prospects avec `relevance_score >= 8`
- Ordre par `relevance_score DESC` (meilleurs d'abord)
- Limite réduite à 30 profils/cycle (économise les visites)

#### C4: "IT Director" banni (IMPLEMENTÉ dans autoresearch_service.py)
- Supprimé de TARGET_TITLES et des exemples du prompt
- 41 prospects, score moyen 22, 0 qualifiés → inutile

#### C5: Post-validation des queries Claude (IMPLEMENTÉ dans autoresearch_service.py)
- Rejet automatique des queries > 4 mots
- Rejet des keywords bannies
- Diversité géographique forcée: si toutes les queries sont Singapore, remplace les 2 dernières

#### C6: "Manager" seul = rejet (IMPLEMENTÉ dans jobs.py)
- Si le seul signal tech dans le headline est "manager" → rejeté
- Élimine "Office Manager", "Account Manager", etc.

#### C7: Title length gate (IMPLEMENTÉ dans scoring_service.py)
- Titre < 3 chars ou > 200 chars → score title = 0.0

#### C8: TARGET_TITLES enrichi (IMPLEMENTÉ dans scoring_service.py)
- Ajouté: "vp product", "vp technology", "head of technology", "chief product officer", "co-founder", "director of engineering", "engineering manager", "tech lead", "product director", "digital director"
- Supprimé: "it director"

#### C9: Deep screen success rate tracking (IMPLEMENTÉ dans jobs.py)
- Après deep screening, calcule le % de profils avec company extraite
- Si < 30% sur ≥ 5 profils → alerte CSS selectors cassés

---

## Objectif
Trouver des décideurs tech à Singapour et en Asie du Sud-Est qui pourraient externaliser du développement digital auprès d'Okoone.

## Profil cible
- Titres: CTO, VP Engineering, VP Product, VP Technology, Head of Engineering, Head of Product, Head of Technology, Technical Director, Engineering Manager, Technical Co-founder, Chief Digital Officer, Co-founder (tech), Product Director, Digital Director, Tech Lead, Director of Engineering
- Entreprises: Startups en croissance (Series A-C) ou PME tech (50-500 employés)
- Secteurs: fintech, healthtech, edtech, SaaS B2B, e-commerce, proptech, logistics-tech, traveltech, insurtech, agritech, climate-tech, digital banking
- Localisation: Singapore (priorité 1), Vietnam (priorité 2), Thailand, Indonesia, Philippines, Malaysia, Cambodia

---

## PHASE 0: HEALTH CHECK (avant tout cycle)

### 1. Valider le deep screening
- Deep-screen 1 profil connu (username hardcodé: `benoitddlp`)
- Vérifier que `current_company`, `current_title`, `experience` sont non-vides
- Si échec → ABORT le cycle entier. Logger l'erreur.

### 2. Valider la session LinkedIn
- Vérifier `is_session_valid()`
- Si non → tenter `_auto_relogin()`
- Si échec → ABORT

---

## PHASE 1: FILTRAGE À L'EXTRACTION (avant insertion en DB)

### Filtres d'extraction (dans jobs.py à l'insertion)
Rejeter AVANT insertion si:
- `headline` commence par "•" (badge connexion LinkedIn)
- `headline` est vide
- `headline` contient "degree connection"
- `headline` ne contient AUCUN signal tech parmi: cto, cio, cdo, cpo, vp, vice president, director, head of, chief, founder, co-founder, engineer, developer, tech, digital, product, software, data, cloud, platform, ai, machine learning, devops, architect
- **v7: `headline` contient UNIQUEMENT "manager" sans autre signal tech → rejeter** (élimine "Office Manager", "Account Manager", etc.)

### Company extraction à l'insertion
Parser le headline pour extraire la company via séparateurs: " at ", " @ ", " | ", " - ", ", "

---

## PHASE 2: HARD GATES (dans scoring_service.py)

### Gate 1: Garbage name filter
`is_garbage_name()` → score = 0.0

### Gate 2: Garbage headline filter
`is_garbage_headline()` → score = 0.0

### Gate 3: Données minimum
Score plafonné à 15 si < 2 champs parmi (company, location, titre)

### Gate 4: Not-deep-screened cap (v7)
Score plafonné à **18** si `screened_at IS NULL`

### Gate 5: Anti-compétiteur
Rejeter si headline/company contient: "outsourcing", "offshore development", "software house", "dev shop", "nearshore", "freelance", "independent consultant", "self-employed", "IT consulting", "technology consulting"

### Gate 6: Title length (v7)
Title/headline < 3 caractères ou > 200 caractères → title_score = 0.0

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

### TARGET_TITLES
```
cto, vp engineering, vp product, vp technology, head of engineering,
head of digital, head of product, head of technology, chief digital,
chief technology, chief product officer, founder, co-founder, ceo,
technical director, director of engineering, engineering manager,
tech lead, product director, digital director
```

### Seuil de qualification: 40

---

## PHASE 4: DEEP SCREENING

### Pré-filtre (v7)
Ne deep-screen que les prospects avec:
- `relevance_score >= 8`
- Ordre par `relevance_score DESC`
- Limite: **30** profiles par cycle

### Session validation
- Détecter pages login (≥2 signaux parmi: "Welcome back", "Sign in", "Forgot password", "Join now", "User Agreement")
- Si login détecté → arrêter tout le deep screening
- Si experience a toutes les companies vides → ne pas sauvegarder l'experience garbage

### Success rate tracking (v7)
- `deep_screen_success_rate = (profiles avec company) / (total deep-screened)`
- Si < 30% sur ≥ 5 profils → alerte CSS selectors cassés

---

## PHASE 5: STRATÉGIE DE RECHERCHE v7

### RÈGLE D'OR: Maximum 3 mots-clés par query
Toutes les queries > 3 mots ont produit 0 qualifiés.

### PAGINATION: Maximum 3 pages par query

### PRIORITÉ 0: Queries par entreprise connue en SEA

**Singapore (funded startups, 2025-2026):**
- "CTO Carousell", "CTO Xendit", "CTO PropertyGuru", "CTO StashAway"
- "CTO Funding Societies", "CTO Endowus", "CTO PatSnap", "CTO Aspire"
- "VP Engineering ShopBack", "CTO Ninja Van", "CTO Advance Intelligence"
- "CTO Nium", "CTO Carro", "CTO Glints", "CTO Intellect"
- "CTO Hypotenuse AI", "CTO Prism+", "CTO Coda Payments", "CTO Doctor Anywhere"
- "CTO StaffAny", "CTO Spenmo", "CTO Volopay", "CTO Homage"
- "CTO MariBank", "CTO GXS Bank", "CTO Trust Bank"

**Indonesia:**
- "CTO Traveloka", "CTO Bukalapak", "CTO Akulaku"
- "CTO Kopi Kenangan", "CTO eFishery", "CTO Stockbit"
- "CTO Amartha", "CTO JULO", "CTO Bibit", "CTO Ajaib", "CTO Mekari"

**Vietnam:**
- "CTO VNPay", "CTO MoMo", "CTO Tiki", "CTO Sky Mavis"
- "CTO KiotViet", "CTO Base.vn"
- "CTO Trusting Social", "CTO Manabie", "CTO Homebase", "CTO VNLIFE"

**Thailand/Philippines/Malaysia:**
- "CTO Bitkub", "CTO Opn", "CTO Flash Express", "CTO Ascend Money"
- "CTO LINE MAN", "CTO Finnomena"
- "CTO Mynt", "CTO PayMongo", "CTO Kumu"
- "CTO Carsome", "CTO GoGet", "CTO Aerodyne"

### PRIORITÉ 1: Queries minimales (2-3 mots + location filter)
- "CTO" + location:Singapore
- "CTO" + location:Vietnam
- "CTO" + location:"Ho Chi Minh City"
- "CTO" + location:Bangkok
- "CTO" + location:Jakarta
- "CTO" + location:"Kuala Lumpur"
- "CTO" + location:Manila
- "VP Engineering" + location:Singapore
- "VP Product" + location:Singapore
- "VP Technology" + location:Singapore
- "Head Engineering" + location:Singapore
- "Engineering Manager" + location:Singapore
- "Technical Director" + location:Singapore
- "Head of Product" + location:Singapore
- "Co-founder" + location:Singapore
- "Director Engineering" + location:Singapore

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
- "directeur technique" + location:Singapore
- "French CTO" + location:Vietnam

### QUERIES BANNIES
- Toute query > 4 mots (auto-rejetée par validation post-génération)
- "IT Director" @ toute location (auto-rejetée)
- Toute query avec "hiring", "developers", "scaling" dans les keywords
- Toute query déjà utilisée 2+ fois avec 0 qualifiés

### DIVERSITÉ GÉOGRAPHIQUE FORCÉE (v7)
Distribution minimale par cycle de 5 queries (enforced par code):
- 2 queries Singapore
- 1 query Vietnam OU Indonesia
- 1 query Thailand, Philippines, OU Malaysia
- 1 query libre (company-first recommandé)

Si Claude génère uniquement des queries Singapore, le code remplace automatiquement les 2 dernières.

---

## PHASE 6: MÉTRIQUES DE SUCCÈS

| Métrique | Cible v7 | Actuel | Comment mesurer |
|----------|----------|--------|-----------------|
| Human approval rate | > 15% | ~6% | MÉTRIQUE PRINCIPALE |
| Deep screen success rate | > 50% | ~0% | company extraite / total deep-screened |
| Garbage rate in DB | < 10% | 75% | Headline gate + sanitization |
| Prospects avec company | > 50% | 0% | Deep screen fix + headline extraction |
| Score moyen (non-zero) | > 30 | 22.5 (inflated) | Strict scoring + deep screening |
| Qualification rate (>40) | > 15% | 0% | Better data + realistic scoring |
| Novelty rate | > 30% | 32% | Maintenir |
| Diversity (secteurs) | > 3/cycle | 0 | Query rotation + geo diversity |
| Health check pass rate | 100% | N/A | Cycles où le health check passe |

---

## CHANGEMENTS CODE DÉJÀ IMPLÉMENTÉS

### scoring_service.py
1. TARGET_TITLES enrichi (+10 titres, -1 "it director")
2. Not-deep-screened cap à 18
3. Title length gate (< 3 ou > 200 chars)

### jobs.py
1. Health check au démarrage (deep-screen `benoitddlp`)
2. Deep screening pré-filtré par score (>= 8, ORDER BY score DESC, LIMIT 30)
3. Manager-only headline rejection
4. Deep screen success rate tracking + CSS selector alert

### autoresearch_service.py
1. "IT Director" banni du prompt
2. Post-validation: rejet queries > 4 mots
3. Post-validation: rejet keywords bannies
4. Diversité géographique forcée

### À FAIRE: COMMITER ET REDÉMARRER LE SERVICE
```bash
git add app/scheduler/jobs.py app/services/scoring_service.py app/services/autoresearch_service.py
git commit -m "v7: health check, scoring gates, query validation, geo diversity"
# Redémarrer le service
```
