# Cours Git

## Branches

Une branche est un pointeur mobile vers un commit. Créer une branche ne copie rien : on peut donc en ouvrir une par fonctionnalité sans coût, travailler en isolation puis la proposer à la fusion.

## Fusion et rebase

La fusion crée un commit de fusion qui relie deux historiques et conserve la chronologie réelle. Le rebase rejoue les commits d'une branche au-dessus d'une autre pour obtenir un historique linéaire, mais il réécrit les commits : on ne rebase jamais une branche déjà partagée avec d'autres personnes.

```bash
git switch fonctionnalite
git rebase main
```

## Annuler une modification

Pour défaire un commit déjà publié, on utilise git revert, qui ajoute un commit inverse sans toucher à l'historique. Pour abandonner des commits locaux, git reset déplace la branche ; avec l'option --hard il détruit aussi les modifications du répertoire de travail, à manier avec prudence.

## Mettre de côté du travail en cours

La commande git stash enregistre les modifications non validées dans une pile et rétablit un répertoire propre, ce qui permet de changer de branche pour une urgence. On récupère ensuite le travail avec git stash pop.

## Retrouver le commit fautif

git bisect cherche par dichotomie le commit qui a introduit un bogue : on désigne un commit sain et un commit défectueux, puis Git propose des commits intermédiaires à tester jusqu'à isoler le coupable en un nombre logarithmique d'étapes.

## Récupérer un seul commit

git cherry-pick applique sur la branche courante les changements d'un commit choisi d'une autre branche, en créant un nouveau commit. C'est utile pour reporter un correctif sur une branche de maintenance sans fusionner tout le reste.
