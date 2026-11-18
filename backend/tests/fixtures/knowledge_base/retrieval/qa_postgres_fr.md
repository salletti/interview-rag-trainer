---
kind: qa_sheet
language: fr
---

# Fiche de révision PostgreSQL

## Question : À quoi sert un index B-tree et quand ne sert-il à rien ?

Un index B-tree permet de retrouver des lignes par égalité ou par intervalle sans parcourir toute la table. Il devient inutile quand la requête lit une grande partie de la table, car le planificateur préfère alors un parcours séquentiel, ou quand la condition applique une fonction à la colonne indexée.

```sql
CREATE INDEX idx_commandes_client ON commandes (client_id);
CREATE INDEX idx_clients_email_min ON clients (lower(email));
```

## Question : Que fait EXPLAIN ANALYZE ?

EXPLAIN ANALYZE exécute réellement la requête et affiche le plan retenu avec, pour chaque nœud, le temps passé et le nombre de lignes estimé puis réellement lu. Un écart important entre les deux nombres indique des statistiques périmées, que l'on corrige avec ANALYZE.

## Question : Quels sont les niveaux d'isolation des transactions ?

PostgreSQL propose READ COMMITTED par défaut, REPEATABLE READ et SERIALIZABLE. En READ COMMITTED, chaque instruction voit les données validées au moment où elle démarre ; en REPEATABLE READ, toute la transaction travaille sur le même instantané, ce qui évite les lectures non répétables mais peut provoquer des échecs de sérialisation à rejouer.

## Question : Qu'est-ce que le problème N+1 et comment l'éviter avec un ORM ?

Le problème N+1 apparaît quand on charge une liste de N objets puis qu'on déclenche une requête supplémentaire par objet pour lire une relation. On l'évite en chargeant la relation d'avance avec une jointure ou une requête groupée.

```python
stmt = select(Commande).options(selectinload(Commande.lignes))
```

## Question : Comment un VACUUM aide-t-il PostgreSQL ?

Les mises à jour et suppressions laissent des versions mortes de lignes à cause du MVCC. VACUUM les marque comme réutilisables et met à jour la carte de visibilité, ce qui limite le gonflement des tables ; l'autovacuum le lance automatiquement selon des seuils de modifications.

## Question : Comment ajouter une colonne sans bloquer une grande table en production ?

On ajoute la colonne nullable sans valeur par défaut coûteuse, on remplit les valeurs par lots dans des transactions courtes, puis on pose la contrainte NOT NULL une fois les données complètes. Chaque étape est une migration séparée, déployée avec le code qui sait lire les deux états.
