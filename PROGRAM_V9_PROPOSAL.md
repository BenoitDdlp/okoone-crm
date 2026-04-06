## Analyse des tendances

### Pipeline en échec critique

| Métrique | Cycle 58 | Cycle 75 | Cycle 76 | Cycle 77 | Cycle 78 | Tendance |
|----------|----------|----------|----------|----------|----------|----------|
| Human approval | 6.2% | 6.2% | 5.3% | 5.3% | 4.3% | ↘ en baisse |
| Novelty rate | 32.2% | 23.3% | 16.6% | 3.6% | 3.4% | ↘↘ effondrement |
| Avg score | 22.5 | 25.0 | 23.6 | 22.5 | 23.7 | → stagnant |
| Qualification rate | 0% | 2.9% | 0% | 0% | 0% | → zéro |
| Diversity | 0 | 8 | 7 | 0 | 3 | → volatile |

**Diagnostic:** Le pipeline est fondamentalement cassé. Le problème principal n'est PAS le programme — c'est l'implémentation dans le code.

### Cause racine identifiée (CRITIQUE)

L'analyse du code source révèle que **le programme v7 n'est pas implémenté dans le code**:

1. **`garbage_patterns.py` existe mais n'est JAMAIS appelé** — les fonctions `is_garbage_name()`, `is_garbage_headline()`, `is_garbage_location()` sont définies mais zéro import dans le codebase. Résultat: "Join LinkedIn | Not you? Remove photo" obtient un score de 24-42.

2. **Baselines trop généreuses dans le scoring:**
   - `_score_company()` retournait **0.5** (pas 0) quand company est NULL
   - `_score_location()` retournait **0.2** (pas 0) quand location est vide
   - `_score_activity()` retournait **0.3** comme baseline cadeau
   - Résultat: un prospect complètement vide obtient ~22 points juste avec les baselines

3. **Aucune hard gate** — le scoring est 100% "soft" (pondération), jamais "hard" (rejet à 0)

4. **Les locations dans les métriques sont du garbage** ("I am not The Best", "1,510 reactions", "seek to live") — preuve que les filtres garbage ne fonctionnent pas

### Corrections de code implémentées (v9)

**FIX 1:** Garbage gate dans `prospect_service.py` — appel de `is_garbage_name()`, `is_garbage_headline()`, `is_garbage_location()` AVANT stockage en DB. Les "Join LinkedIn" ne seront plus jamais insérés.

**FIX 2:** Baselines corrigées dans `scoring_service.py`:
- Company vide → 0.0 (au lieu de 0.5)
- Location vide → 0.0 (au lieu de 0.2)
- Activity baseline → 0.0 (au lieu de 0.3)
- Company non-vide sans keyword match → 0.3 (baseline modeste)

**FIX 3:** Hard gates dans `score_prospect()`:
- Company ET location vides → score = 0 immédiat
- Titre junior + seniority ≤ 0.1 → score plafonné à 15

---

### Ce qui fonctionne (garder/amplifier)

- "Head of Digital" @ Singapore: 2/23 qualifiés, meilleur ratio
- "VP Technology digital transformation retail" @ Thailand: score moyen 39 (meilleur)
- Le deep_analysis_service (Claude LLM) est bien calibré
- La structure 7 dimensions est saine une fois les baselines fixées

### Ce qui ne fonctionne pas (changer/supprimer)

- Queries Cambodge: les derniers résultats sont presque tous cambodgiens malgré les restrictions
- Saturation: novelty 3.4% = on recycle 96.6% des mêmes profils
- "directeur technique fintech Asie du Sud-Est" → retourne des profils européens
- Queries avec > 4 mots-clés → résultats médiocres
- "IT Director" @ Singapore utilisé 2x, score moyen 28, 0 qualifiés

### Angles sous-explorés (ajouter)

- **Queries par entreprise** (Priorité 1, jamais exécutée en pratique)
- Jakarta, Manila, Kuala Lumpur très peu couverts
- "Engineering Manager", "Technical Co-founder", "Head of Product" peu testés
- Secteurs logistics-tech, traveltech, proptech non ciblés par queries spécifiques
- Queries portfolio (Y Combinator SEA, Antler, Iterative.vc)

---

### Programme proposé (v9)

```
# Prospect Research Program v9

## Changements v7 → v9

### Corrections de code (CRITIQUE — le programme v7 n'était pas implémenté)
- **Garbage gate activé** — Les fonctions garbage_patterns sont maintenant appelées AVANT stockage. "Join LinkedIn", "Provides services", locations garbage = rejet immédiat, jamais insérés en DB.
- **Baselines corrigées** — Company vide=0 (était 0.5), Location vide=0 (était 0.2), Activity=0 (était 0.3). Un prospect vide ne peut plus scorer 22+ points par défaut.
- **Hard gates ajoutés** — Company+Location vides=score 0. Titre junior+seniority≤0.1=plafonné à 15.

### Changements de stratégie
- **Abandon total des queries Cambodge non-C-level** — Les 5 derniers cycles montrent que les queries Cambodge produisent massivement du garbage. Seules 2 queries très ciblées restent.
- **Rotation obligatoire des locations** — Chaque cycle DOIT couvrir au moins 3 pays différents pour casser la saturation.
- **Queries par entreprise** deviennent la priorité absolue — chercher des entreprises, puis leurs décideurs.
- **Suppression des queries épuisées** — Toute query utilisée 2+ fois avec 0 qualifiés est bannie.

---

## Objectif
Trouver des décideurs tech à Singapour et en Asie du Sud-Est qui pourraient externaliser du développement digital auprès d'Okoone.

## Profil cible
- Titres: CTO, VP Engineering, Head of Digital, Head of Product, Head of Engineering, IT Director, Engineering Manager, Technical Co-founder, Technical Director
- Entreprises: Startups en croissance (Series A-C) ou PME tech (50-500 employés)
- Secteurs: fintech, healthtech, edtech, SaaS B2B, e-commerce, proptech, logistics-tech, traveltech
- Localisation: Singapore (priorité 1), Vietnam (priorité 2), Thailand, Indonesia, Philippines, Malaysia
- **Cambodge: autorisé UNIQUEMENT pour le top 5-10%** — C-level ou VP dans une entreprise établie avec budget réel. Tout autre profil cambodgien = rejet automatique.

## Gate de séniorité

### Principe
Un prospect DOIT être un **décideur avec pouvoir d'achat**. Les profils juniors ou mid-level sans autorité budgétaire sont rejetés automatiquement.

### Signaux de profil trop junior (= rejet automatique)
- Titre contient: "Junior", "Intern", "Associate", "Assistant", "Trainee", "Graduate", "Entry-level", "Analyst" (sans "Lead" ou "Senior")
- Moins de 8 ans d'expérience professionnelle estimée
- Aucun rôle de management/direction dans l'historique
- Profil qui ne montre QUE des rôles d'exécutant sans "Senior" ou "Lead"
- Dernier diplôme obtenu il y a < 5 ans sans expérience significative
- **IMPLÉMENTÉ v9: Hard gate dans le code — titre junior + seniority score ≤ 0.1 → score plafonné à 15**

### Signaux de pouvoir d'achat (au moins 1 requis)
- Titre C-level (CTO, CEO, COO, CIO, CPO)
- Titre VP ou Director
- Titre "Head of" ou "Lead" d'une équipe
- Co-founder ou Founder
- Engineering Manager, Technical Lead avec équipe > 5 personnes
- Entreprise a levé des fonds (= budget disponible)

## Gate spécifique Cambodge

### Contexte
Okoone est basé au Cambodge. Des prospects cambodgiens PEUVENT être pertinents — mais seulement l'élite. Le tissu tech/startup cambodgien est petit, donc la majorité des profils n'ont pas le budget.

### Règles pour prospects cambodgiens
Un prospect au Cambodge n'est accepté QUE s'il satisfait TOUS ces critères:
1. **C-level ou VP** dans une entreprise établie (pas juste "Manager" ou "Lead")
2. **Entreprise avec budget réel**: levée de fonds, ou > 20 employés, ou filiale internationale
3. **Parcours international ou entreprise notable**: expérience dans des entreprises reconnues, formation internationale
4. **Pas uniquement un parcours universitaire cambodgien** sans expérience corporate significative

Si un prospect cambodgien ne satisfait pas ces 4 critères → rejet automatique "cambodia_below_top_tier".

## Filtres éliminatoires de données

### Filtres garbage (Phase 0 — AVANT insertion en DB)
**IMPLÉMENTÉ v9: garbage_patterns.py est maintenant appelé dans upsert_from_scrape().**

Un prospect est IMMÉDIATEMENT rejeté si:
- Le nom contient "Join LinkedIn", "Not you?", "Remove photo", "Provides services", "Sign in", "degree connection", "subscribers", "reactions"
- Le nom est un seul mot (mononymie tolérée uniquement si des données solides existent)
- La location contient: "seek to live", "behind live", "reactions", "subscribers", "amazing journey"
- Le headline contient: "Provides services", "Sign in to view", "degree connection", "reactions"

### Seuil minimum de données complètes
Un prospect DOIT avoir au moins 2 sur 3: company non-vide, location non-vide, titre reconnaissable.
**IMPLÉMENTÉ v9: Company vide ET location vide → score = 0 immédiat.**

### Gate de séniorité (Phase 0.5)
**IMPLÉMENTÉ v9: titre junior + seniority ≤ 0.1 → score plafonné à 15.**

### Gate post-deep-screen
Après le deep screening, si le prospect a TOUJOURS:
- `current_company` = null/vide ET non extractible du headline ou experience
- ET `experience_json` vide ou null
- ET `about_text` vide ou null
→ Marqué "insufficient_data" et retiré de la queue de review.

## Gate de review humaine — données minimales obligatoires

### Principe
Un humain ne doit JAMAIS voir un prospect "vide" ou trop junior.

### Conditions de passage en review humaine
Un prospect est AUTOMATIQUEMENT rejeté si:
- `current_company` est null/vide ET aucune company extractible du headline
- `headline` est vide, garbage, ou ne contient aucun titre identifiable
- `location` est vide ET aucune localisation identifiable
- Le prospect n'a été enrichi d'AUCUNE donnée au-delà du nom
- Le titre indique un rôle junior
- Le prospect est au Cambodge et ne satisfait pas les 4 critères top-tier

**Un prospect DOIT avoir AU MINIMUM 2 des 3 champs suivants:**
1. **Company** identifiée (current_company OU extraite du headline/experience)
2. **Titre** identifiable comme poste tech **senior/décisionnel**
3. **Location** identifiable comme ville/pays en SEA

## Signaux positifs (pondérés)
- [FORT] Recrute des développeurs (offres ouvertes = besoin de capacité)
- [FORT] Francophone avec rôle tech en SEA (canal privilégié pour Okoone)
- [FORT] Petite équipe tech (< 20 devs) relative à l'ambition produit
- [FORT] C-level ou VP avec pouvoir d'achat confirmé
- [MOYEN] Background agence/consulting (comprend le modèle d'externalisation)
- [MOYEN] Connexions mutuelles avec l'équipe Okoone
- [MOYEN] Entreprise a levé des fonds récemment (besoin de scaler)
- [FAIBLE] Poste en headline mentionne "building", "scaling", "growing"

## Signaux négatifs (éliminatoires vs pénalisants)
- [ELIMINATOIRE] Freelancer ou service provider
- [ELIMINATOIRE] Entreprise > 1000 employés avec équipe tech > 50 personnes
- [ELIMINATOIRE] Pure consulting/outsourcing/dev agency (concurrent direct)
- [ELIMINATOIRE] Profil sans aucune expérience tech vérifiable
- [ELIMINATOIRE] Location hors Asie-Pacifique
- [ELIMINATOIRE] Données insuffisantes: company vide ET location vide → **score 0 automatique (v9)**
- [ELIMINATOIRE] Profil junior — titre junior, < 8 ans expérience, aucun rôle de direction → **plafonné à 15 (v9)**
- [ELIMINATOIRE] Prospect cambodgien ne satisfaisant pas les 4 critères top-tier
- [PENALISANT] Profil marketing/sales pur sans composante tech
- [PENALISANT] Entreprise pre-revenue sans financement
- [PENALISANT] Formation uniquement locale sans parcours international — malus de -15 points

## Stratégie de recherche v9

### PRIORITÉ 1: Queries par entreprise connue (NOUVELLE PRIORITÉ ABSOLUE)
Chercher d'abord des ENTREPRISES tech en SEA, puis trouver leurs décideurs:
- "[Company name] CTO" — pour chaque entreprise identifiée
- Listes d'entreprises à miner: Y Combinator SEA batch, Antler portfolio Singapore, Iterative.vc portfolio, SEA startup funding announcements
- "Series A Singapore 2025" → identifier entreprises → chercher "[company] CTO"
- "startup hiring engineers Singapore" → lister entreprises → chercher décideurs

### PRIORITÉ 2: Queries titre + lieu (PRÉCIS, ROTATIF)
Chaque cycle DOIT couvrir au minimum 3 pays différents. Rotation obligatoire:

**Cycle pair — Singapore + Indonesia + Vietnam:**
- "CTO" @ Singapore
- "Head of Engineering" @ Jakarta
- "VP Engineering" @ Ho Chi Minh City

**Cycle impair — Thailand + Philippines + Malaysia:**
- "CTO" @ Bangkok
- "Head of Product" @ Manila
- "Engineering Manager" @ Kuala Lumpur

**Titres à alterner (ne pas répéter le même titre 2 cycles de suite):**
- CTO, VP Engineering, Head of Engineering, Head of Digital, Head of Product
- Engineering Manager, Technical Director, Technical Co-founder
- IT Director (seulement si ≥ 50 employés comme filtre)

### PRIORITÉ 3: Queries sectorielles
- "CTO fintech" @ Singapore
- "Head of Engineering healthtech" @ Bangkok
- "VP Engineering SaaS" @ Ho Chi Minh City
- "CTO edtech" @ Jakarta
- "Technical Director proptech" @ Kuala Lumpur
- "CTO logistics tech" @ Manila
- "Engineering Manager traveltech" @ Singapore

### PRIORITÉ 4: Queries francophones SEA
- "CTO" + language filter: French + location: Singapore
- "French CTO Singapore"
- "directeur technique Singapore" (PAS "Asie du Sud-Est" qui retourne des Européens)

### PRIORITÉ 5: Queries Cambodge top-tier UNIQUEMENT
**Maximum 1 query Cambodge par cycle. Uniquement:**
- "CEO" OR "CTO" OR "Managing Director" + location:"Cambodia" + company > 50 employés
- "[grande entreprise internationale] Cambodia" (filiales)
- NE JAMAIS chercher de titres mid-level au Cambodge

### QUERIES BANNIES (0 qualifiés sur 2+ utilisations)
- "IT Director" @ Singapore (score 28, 0/43 qualifiés en 2 utilisations)
- "directeur technique" @ Singapore (score 27, 0/15)
- "CTO SaaS" @ Singapore (score 24, 0/18)
- "Technical Director" @ Kuala Lumpur (score 24, 0/12)
- "directeur technique fintech Asie du Sud-Est" (retourne des Européens)
- Toute query avec > 4 mots-clés
- Toute query Cambodge non-C-level

### RÈGLES DE FORMULATION
- Maximum 3-4 mots-clés par query
- Toujours inclure une localisation explicite
- Alterner titres ET localisations à chaque cycle
- Préférer les filtres LinkedIn natifs au texte libre
- Cambodge: uniquement C-level/VP + entreprise établie
- **NOUVEAU v9: Ne JAMAIS réutiliser une query identique 2 cycles de suite**
- **NOUVEAU v9: Si novelty_rate < 20%, changer TOUTES les queries du prochain cycle**

## Validation des données (RENFORCÉE v9)
Avant de qualifier un prospect, vérifier que:
- Le nom ressemble à un vrai nom (2+ mots, pas de garbage LinkedIn UI)
- La location est une vraie ville/pays en SEA
- L'entreprise est identifiée (non-null, non-vide)
- Le titre est un vrai titre de poste senior/décisionnel
- Au moins 2/3 champs critiques sont remplis
- **v9: Company vide → score company = 0 (pas 0.5)**
- **v9: Location vide → score location = 0 (pas 0.2)**
- **v9: Garbage patterns appliqués AVANT stockage**

### Validation AVANT review humaine
**Aucun prospect ne passe en review humaine sans satisfaire TOUTES ces conditions:**
1. Au moins 2 sur 3: company, location, titre — avec des données RÉELLES
2. Si deep-screené: au moins 1 donnée enrichie
3. Le score est > 0 (a passé tous les hard gates)
4. Le prospect n'est PAS marqué "insufficient_data"
5. Le prospect n'est PAS marqué "too_junior"
6. Le prospect n'est PAS marqué "cambodia_below_top_tier"
7. Le titre indique un rôle de décideur

## Marchés cibles (ordre de priorité)
1. **Singapore** — priorité maximale
2. **Vietnam** — marché tech en forte croissance (Ho Chi Minh City, Hanoi)
3. **Thailand** — hub régional (Bangkok)
4. **Indonesia** — plus grand marché SEA (Jakarta)
5. **Philippines** — écosystème émergent (Manila, Makati)
6. **Malaysia** — hub tech (Kuala Lumpur)
7. **Cambodia** — UNIQUEMENT top 5-10%: C-level dans entreprises établies

### Marchés EXCLUS
- Tout pays hors Asie-Pacifique

## Métriques de succès
- Taux d'approbation humaine: cible > 20%
- Taux de qualification: cible > 30%
- Nouveauté: > 50% par run (si < 20%, changer toutes les queries)
- Diversité sectorielle: minimum 3 secteurs par cycle
- Diversité géographique: minimum 3 pays par cycle (NOUVEAU v9)
- Données complètes: > 80% des prospects avec company + location valides
- Taux de prospects "insufficient_data" présentés en review: cible = 0%
- Taux de prospects juniors présentés en review: cible = 0%
- Taux de garbage passant les filtres: cible = 0% (NOUVEAU v9)
- % de prospects avec signal de pouvoir d'achat: cible > 80%

## Historique feedback humain
| Prospect | Verdict | Feedback | Action |
|----------|---------|----------|--------|
| Multiple cambodian prospects | reject | Le Cambodge n'a pas le tissu tech/startup qu'on cible. | Cambodge: seul le top 5-10% est accepté |
| Anurag Yagnik | reject | Tu as rien scrapé sur cette personne. | Gate de review humaine: "insufficient_data" |
| Voneat Pen | reject | Beaucoup trop junior + une université au Cambodge = 0 budget. | Gate de séniorité + gate Cambodge renforcée |
| Join LinkedIn entries | non-review | Garbage LinkedIn UI artefacts scoring 24-42 | **v9: garbage_patterns.py activé, baselines corrigées** |
| Prospects @ None company | non-review | Prospects sans company obtenant des scores de 22+ | **v9: company vide = score 0, hard gate company+location** |
```

---

### Prédictions

1. **Garbage rate → ~0%** — Les "Join LinkedIn", locations garbage, headlines garbage seront rejetés avant stockage. Impact immédiat.

2. **Avg score redistribué** — Les scores vont se polariser: les vrais prospects monteront (plus de place dans la distribution), les faux descendront à 0. L'avg_score va baisser à court terme (beaucoup de 0), mais le score MÉDIAN des prospects qualifiés devrait monter.

3. **Novelty rate → amélioration progressive** — La rotation obligatoire des locations et le bannissement des queries épuisées devraient casser le cycle de saturation. Cible: >30% d'ici 3 cycles.

4. **Human approval rate → cible 15-20% dans 5 cycles** — Les hard gates éliminent les faux positifs les plus flagrants. Les vrais décideurs tech SEA restent. Le ratio qualité/bruit s'améliore mécaniquement.

5. **Le programme seul ne suffit pas** — Ces changements corrigent l'implémentation. Si l'approval rate ne monte pas après 3 cycles avec le code corrigé, le problème est dans la QUALITÉ des données LinkedIn (scraping), pas dans le scoring.
