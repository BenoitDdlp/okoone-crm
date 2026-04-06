# Prospect Research Program v5

## Changements majeurs v4 → v5

### CORRECTION CRITIQUE 1: Kill-switch post-deep-screen
Un prospect deep-screené qui a TOUJOURS company=None ET location non-SEA → score=0, status=rejected.
v4 les laissait à 22.5 dans le pipeline indéfiniment. v5 les élimine.

### CORRECTION CRITIQUE 2: Filtre géographique strict dans les queries
v4 laissait des queries sans location ou avec des locations vagues ("Southeast Asia").
v5 exige une VILLE ou un PAYS précis pour chaque query. Cambodge exclu des queries (trop de bruit, pas la cible).

### CORRECTION CRITIQUE 3: Headline gate renforcé — décideurs seulement
v4 laissait passer tout profil avec un mot tech ("engineer", "developer").
v5 exige un signal de SÉNIORITÉ dans le headline (cto, vp, head, director, chief, founder, lead, manager + tech).
Un "Senior Backend Developer" n'est pas un décideur et ne doit pas entrer dans le pipeline.

### CORRECTION CRITIQUE 4: Non-Latin script filter
Les profils avec des noms en caractères chinois (李奶奶, 张立斌), coréens, etc. ne sont pas la cible.
v5 rejette à l'extraction les noms qui ne contiennent pas au moins 50% de caractères latins.

### CORRECTION CRITIQUE 5: Deep-screen threshold relevé à 20
v4 deep-screenait tout prospect avec score >= 8, gaspillant le quota (50/jour) sur des profils sans signal.
v5 exige score >= 20 pour mériter un deep-screen.

### CORRECTION CRITIQUE 6: Queries Cambodge supprimées
Le cycle 75 a remonté ~30 profils cambodgiens — aucun n'est la cible.
v5 supprime "Phnom Penh" et "Cambodia" des locations de recherche. Ils restent en tier 0.7 au scoring si trouvés par hasard.

---

## Objectif
Trouver des décideurs tech à Singapour et en Asie du Sud-Est qui pourraient externaliser du développement digital auprès d'Okoone.

## Profil cible
- Titres: CTO, VP Engineering, Head of Engineering, Head of Product, Head of Digital, Technical Director, Engineering Manager, Technical Co-founder, Chief Digital Officer, Chief Product Officer
- Entreprises: Startups en croissance (Series A-C) ou PME tech (50-500 employés)
- Secteurs: fintech, healthtech, edtech, SaaS B2B, e-commerce, proptech, logistics-tech, traveltech, insurtech, agritech
- Localisation: Singapore (priorité 1), Vietnam (priorité 2), Thailand, Indonesia, Philippines, Malaysia
- Anti-cible: freelancers, agences dev, outsourcing, consulting IT, profils sans company identifiable

---

## PHASE 0: FILTRAGE À L'EXTRACTION (avant insertion en DB)

### Filtres d'extraction
Rejeter AVANT insertion si:
- `linkedin_username` commence par "ACoA" (ID interne LinkedIn)
- `full_name` est None/vide ou n'a pas d'espace (= username)
- `full_name` contient < 50% caractères latins (a-z, accents, hyphens, espaces) → élimine noms en caractères chinois, coréens, arabes
- `full_name` contient un pattern garbage: `Provides services|Status is|View.*profile|subscribers|reactions|3rd\+|degree connection|Join LinkedIn|LinkedIn Member|• `
- `headline` est UNIQUEMENT "• Xnd/rd/th+ degree connection"
- `headline` ne contient AUCUN signal de décideur tech (voir SENIORITY_TECH_SIGNALS ci-dessous)
- `location` matche un pattern UI LinkedIn: `seek to live|currently behind|amazing journey|subscribers|reactions`

### SENIORITY_TECH_SIGNALS (pour headline gate)
Le headline doit contenir AU MOINS UN de ces patterns:
- Titres décideurs: cto, cio, cdo, cpo, vp, vice president, director, head of, chief, founder, co-founder
- Titres tech seniors: tech lead, architect, engineering manager, product manager, product owner
- EXCLUS (trop junior, pas décideurs): engineer, developer, designer, analyst, coordinator, assistant, intern, trainee, student

### Métriques de filtrage
Logger: `filtered_at_extraction / total_raw_results`. Cible: 30-60%.

---

## PHASE 1: HARD GATES (après insertion, avant scoring)

### Gate 1: Garbage data filter
Score = 0 immédiat si:
- `full_name` garbage (is_garbage_name)
- `headline` garbage (is_garbage_headline)
- `headline` vide ET `location` vide ET `current_company` vide (= fantôme)

### Gate 2: Données minimum
Score = 0 si les 3 conditions sont TOUTES vraies:
- `current_company` est null/vide
- `location` est vide ou non-identifiable
- `headline` ne contient aucun mot de TARGET_TITLES

### Gate 3: Plafond non-deep-screened
Non deep-screened (screened_at = null) → plafonné à 18.

### Gate 4: Anti-compétiteur
Score = 0 si `current_company` ou `headline` contient:
- "outsourcing", "offshore development", "software house", "dev shop", "nearshore"
- "freelance", "independent consultant", "self-employed", "solopreneur"
- "IT consulting", "technology consulting" (sauf si titre = CTO/VP)

### Gate 5 (NOUVEAU v5): Kill-switch post-deep-screen
Si `screened_at` IS NOT NULL (= a été deep-screené) ET `current_company` IS NULL/vide ET location n'est pas dans LOCATION_TIERS SEA:
→ Score = 0, status = "rejected_post_screen"
Raison: un prospect deep-screené sans company exploitable ne vaut pas le coût de review humaine.

---

## PHASE 2: SCORING (après les gates)

### Pondérations

| Critère | Poids | Raison |
|---------|-------|--------|
| title_match | 30 | Le titre est le signal #1 |
| company_fit | 25 | Company identifiée = prospect réel |
| seniority | 15 | Niveau décisionnel |
| industry | 15 | Secteur cible |
| location | 10 | Géographie |
| completeness | 3 | Données disponibles |
| activity | 2 | Signal faible |

### Baselines strictes
- company vide → company_fit = 0.0
- location vide → location = 0.0
- location inconnue (pas dans tiers) → location = 0.05
- activity baseline → 0.1

### Seuil de qualification: 40

### TARGET_TITLES
cto, vp engineering, vp product, vp technology, head of engineering, head of digital, head of product, head of technology, chief digital, chief technology officer, chief product officer, founder, co-founder, ceo, technical director, director of engineering, engineering manager, tech lead, product director, digital director

### TARGET_INDUSTRIES
fintech, healthtech, edtech, saas, ecommerce, e-commerce, digital, technology, software, startup, ai, blockchain, proptech, logistics, traveltech, insurtech, agritech

### Signaux positifs (bonus post-scoring, max +15 points)
- [+10] Recrute des développeurs (offres visibles dans experience/about)
- [+8] Francophone avec rôle tech en SEA
- [+5] Petite équipe tech (< 20 devs)
- [+5] Entreprise a levé des fonds récemment
- [+3] Background agence/consulting passé
- [+2] Headline contient "building", "scaling", "growing"

### Signaux négatifs
- [ÉLIMINATOIRE → score=0] Freelancer/service provider
- [ÉLIMINATOIRE → score=0] Agence dev/outsourcing (concurrent)
- [-20] Entreprise > 1000 employés avec équipe tech > 50
- [-15] Location hors Asie-Pacifique
- [-10] Profil marketing/sales sans composante tech

---

## PHASE 3: STRATÉGIE DE RECHERCHE v5

### RÈGLE D'OR: Maximum 3 mots-clés par query + location OBLIGATOIRE
Chaque query DOIT avoir un champ location. Pas de query sans location.

### QUERIES SUPPRIMÉES (par rapport à v4)
- Toute query ciblant Cambodge/Phnom Penh (trop de bruit, 0 qualifié)
- "IT Director" (43 prospects, 0 qualifié — trop générique)
- Queries francophones en dehors de Singapore (0 résultat)

### PRIORITÉ 0: Queries par entreprise connue en SEA (RENFORCÉ)
Rotation de 3-4 entreprises par cycle.

**Singapore (priorité absolue):**
- "CTO Carousell", "CTO Xendit", "CTO PropertyGuru", "CTO StashAway"
- "CTO Funding Societies", "CTO Endowus", "CTO PatSnap", "CTO Aspire"
- "VP Engineering ShopBack", "CTO Ninja Van", "CTO Advance Intelligence"
- "CTO Nium", "CTO Carro", "CTO Glints", "CTO Intellect"

**Indonesia:**
- "CTO GoTo", "CTO Traveloka", "CTO Bukalapak", "CTO Akulaku"
- "CTO eFishery", "CTO SiCepat", "CTO Stockbit"

**Vietnam:**
- "CTO VNPay", "CTO MoMo", "CTO Tiki", "CTO Sky Mavis"
- "CTO KiotViet", "CTO Got It"

**Thailand/Philippines/Malaysia:**
- "CTO Bitkub", "CTO Omise", "CTO Flash Express" (Thailand)
- "CTO Mynt", "CTO PayMongo" (Philippines)
- "CTO Carsome", "CTO Aerodyne" (Malaysia)

### PRIORITÉ 1: Titre + ville (queries courtes)
- "CTO" + location:Singapore
- "VP Engineering" + location:Singapore
- "Head Engineering" + location:Singapore
- "Head Digital" + location:Singapore ← GARDER (seule query avec qualifiés, best=74)
- "CTO" + location:Vietnam
- "CTO" + location:"Ho Chi Minh City"
- "CTO" + location:Bangkok
- "CTO" + location:Jakarta
- "CTO" + location:"Kuala Lumpur"
- "CTO" + location:Manila
- "VP Engineering" + location:Bangkok
- "VP Engineering" + location:Jakarta
- "Head Digital" + location:Bangkok
- "Technical Director" + location:Singapore
- "CDO" + location:Singapore (NOUVEAU — Chief Digital Officer sous-exploré)
- "CPO" + location:Singapore (NOUVEAU — Chief Product Officer sous-exploré)

### PRIORITÉ 2: Industrie + titre + ville (rotation par cycle)
Cycle N: Singapore + fintech/healthtech
- "CTO fintech" + location:Singapore
- "CTO healthtech" + location:Singapore

Cycle N+1: Vietnam + SaaS
- "CTO SaaS" + location:Vietnam
- "VP Engineering" + location:"Ho Chi Minh City"

Cycle N+2: Thailand + e-commerce
- "CTO ecommerce" + location:Bangkok
- "Head Digital" + location:Bangkok

Cycle N+3: Indonesia + fintech
- "CTO fintech" + location:Jakarta
- "CTO proptech" + location:Jakarta

Cycle N+4: Malaysia/Philippines
- "CTO" + location:"Kuala Lumpur"
- "CTO" + location:Manila

### PRIORITÉ 3: Queries francophones (Singapore seulement)
- "French CTO" + location:Singapore
- "directeur technique" + location:Singapore

### QUERIES BANNIES
- Toute query > 4 mots
- Toute query sans location
- "IT Director" (trop générique, 0/43 qualifiés)
- Toute query ciblant Cambodia/Phnom Penh
- "directeur technique fintech Asie du Sud-Est" (retourne des Européens)
- Toute query déjà utilisée 2+ fois avec 0 qualifiés

### ROTATION PAR CYCLE
Chaque cycle doit:
1. Utiliser au moins 2 localisations différentes
2. Utiliser au moins 2 titres différents
3. Ne pas répéter une query identique des 5 derniers cycles
4. Inclure 1 query PRIORITÉ 0 (company-first) + 1 PRIORITÉ 1 (titre+ville)
5. Singapore doit apparaître dans >= 50% des queries

---

## PHASE 4: VALIDATION POST-DEEP-SCREEN

1. Extraire `current_company` de experience_json[0]["company"] si manquant
2. Extraire `current_title` de experience_json[0] ou headline si manquant
3. Vérifier que `location` est une vraie ville/pays
4. Si deep-screené mais company toujours NULL et location non-SEA → REJETER (Gate 5)
5. Si tout est rempli → re-scorer

### Company extraction fallback chain
1. experience_json[0]["company"]
2. Parser headline: "CTO at Company" → "Company"
3. Parser headline: "CTO | Company" → "Company"
4. Parser headline: "CTO, Company" → "Company"
5. Si tout échoue → appliquer Gate 5

---

## PHASE 5: MÉTRIQUES DE SUCCÈS

| Métrique | Cible v5 | Actuel (v4) |
|----------|----------|-------------|
| Human approval rate | > 15% | 6.2% |
| Prospects avec company | > 50% | ~0% |
| Prospects avec location SEA | > 60% | ~10% |
| Qualification rate (score > 40) | > 15% | 2.9% |
| Novelty rate | > 30% | 23% |
| Diversity (secteurs) | > 3 par cycle | 8 |
| Garbage rate | < 5% | ~30% |

---

## CHANGEMENTS CODE REQUIS (par ordre d'implémentation)

### C1: jobs.py — Headline gate renforcé (décideurs seulement)
Remplacer _TECH_SIGNALS par _DECISION_MAKER_SIGNALS qui exclut "engineer", "developer", "software", etc.

### C2: jobs.py — Non-Latin script filter à l'insertion
Rejeter les noms avec < 50% caractères latins.

### C3: scoring_service.py — Gate 5 kill-switch post-deep-screen
Deep-screené + no company + no SEA location → score = 0.

### C4: jobs.py — Deep-screen threshold à 20 (was 8)

### C5: jobs.py — Location obligatoire dans les queries générées
Rejeter toute query sans location field.
