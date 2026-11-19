---
kind: qa_sheet
language: fr
---

# Fiche de révision Redis

## Question : Quels types de données propose Redis au-delà des chaînes ?

Redis propose les listes, les ensembles, les ensembles triés, les tables de hachage et les flux. Chaque type a ses commandes atomiques, ce qui permet d'implémenter des files, des classements ou des compteurs sans verrou applicatif.

## Question : Comment Redis expire-t-il une clé ?

On associe une durée de vie à la clé avec EXPIRE ou l'option EX de SET. À l'échéance, la clé est supprimée paresseusement lors d'un accès ou par un échantillonnage périodique en arrière-plan.

```text
SET session:42 "données" EX 3600
```

## Question : Quelle est la différence entre RDB et AOF pour la persistance ?

Le RDB enregistre des instantanés complets à intervalles réguliers : il est compact et rapide à recharger mais peut perdre les dernières écritures. L'AOF journalise chaque écriture et se rejoue au redémarrage, au prix d'un fichier plus gros.

## Question : Comment éviter la saturation de la mémoire de Redis ?

On fixe une limite avec maxmemory et une politique d'éviction, par exemple allkeys-lru, qui supprime les clés les moins récemment utilisées quand la limite est atteinte. Sans politique, les écritures sont refusées une fois la mémoire pleine.
