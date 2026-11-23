# Cours asyncio

## Introduction

Asyncio permet d'écrire du code concurrent avec une seule boucle d'événements. Ce cours présente les coroutines, les tâches et la boucle.

## Coroutines

### Définir une coroutine

Une coroutine se définit avec async def et ne s'exécute que lorsqu'on l'attend avec await ou qu'on la planifie.

```python
async def saluer(nom):
    await asyncio.sleep(0.1)
    return f"bonjour {nom}"
```

### Exécuter une coroutine

Le point d'entrée habituel est asyncio.run, qui crée la boucle, exécute la coroutine principale puis ferme proprement la boucle.

## Tâches et annulation

### Créer une tâche

Une tâche enveloppe une coroutine et la planifie immédiatement sur la boucle. On l'obtient avec asyncio.create_task, et la boucle l'exécute dès que la coroutine courante cède la main à un point await, sans attendre la fin de celle qui l'a créée. Il faut conserver une référence à la tâche pour éviter qu'elle soit collectée.

### Annuler une tâche

L'annulation passe par la méthode cancel, qui lève CancelledError à l'intérieur de la coroutine au prochain point await. La coroutine peut intercepter cette exception pour libérer ses ressources, puis doit la relancer afin que l'annulation se propage correctement jusqu'à l'appelant qui attend la tâche.

### Groupes de tâches

TaskGroup lance plusieurs tâches et attend leur fin comme un bloc. Si l'une échoue, les autres sont annulées et les exceptions sont regroupées dans un ExceptionGroup, ce qui rend la gestion des erreurs plus fiable qu'avec gather lorsque plusieurs tâches peuvent échouer ensemble dans le même bloc.

## Boucle d'événements

La boucle d'événements est le planificateur d'asyncio : elle attend que des opérations d'entrée-sortie soient prêtes, puis reprend les coroutines suspendues correspondantes dans l'ordre où leurs événements arrivent, sans jamais préempter une coroutine qui n'a pas atteint un point await.

Une coroutine qui effectue un long calcul sans jamais céder la main bloque toute la boucle : aucune autre tâche ne progresse, les délais d'attente ne se déclenchent pas et les connexions réseau restent sans réponse tant que le calcul n'est pas terminé.

Pour les calculs intensifs, on les confie à un exécuteur de processus ou de threads grâce à run_in_executor, ce qui laisse la boucle disponible pour les autres coroutines pendant que le travail se déroule en parallèle ailleurs dans le programme.

Le mode debug de la boucle signale les coroutines jamais attendues et les callbacks trop lents, ce qui aide à repérer les blocages. On l'active avec la variable d'environnement PYTHONASYNCIODEBUG ou avec le paramètre debug de asyncio.run.

Enfin, il faut éviter d'appeler du code bloquant comme time.sleep ou une bibliothèque réseau synchrone depuis une coroutine : on utilise les équivalents asynchrones, ou on déporte l'appel dans un thread afin de préserver la réactivité générale du programme, y compris lorsque la charge augmente fortement et que de nombreuses connexions arrivent en même temps.
