# Concurrence en Python

## Le GIL et les threads

Le verrou global de l'interpréteur, le GIL, garantit qu'un seul thread exécute du bytecode Python à un instant donné dans CPython. Les threads restent utiles pour les entrées-sorties, car le GIL est relâché pendant une lecture réseau ou disque, mais ils n'accélèrent pas un calcul pur en Python.

## Exécuter du code bloquant depuis une application asyncio

Une application asyncio repose sur une boucle d'événements unique qui reprend tour à tour les coroutines prêtes. Cette boucle ne peut reprendre une coroutine que lorsque la coroutine courante cède la main à un point await. Toute fonction synchrone qui dure, qu'il s'agisse d'un appel réseau bloquant, d'une lecture de fichier volumineux, d'une requête à une base de données par un pilote synchrone ou d'un calcul long, empêche donc toutes les autres coroutines de progresser jusqu'à ce qu'elle se termine.

Le symptôme typique est une latence qui explose sous la charge : un serveur web asynchrone qui appelle une bibliothèque HTTP synchrone dans un gestionnaire de requête ne traite plus qu'une requête à la fois. Les délais d'attente ne se déclenchent plus à l'heure, les battements de cœur d'une connexion de messagerie partent en retard et le courtier finit par fermer la connexion, alors que le processeur n'est presque pas utilisé.

La première règle est de préférer une bibliothèque nativement asynchrone quand elle existe : un client HTTP asynchrone, un pilote de base de données asynchrone, des primitives de temporisation comme asyncio.sleep à la place de time.sleep. Quand ce n'est pas possible, parce que la bibliothèque n'existe qu'en version synchrone ou que le code métier est déjà écrit ainsi, il faut déporter l'appel bloquant hors de la boucle.

La fonction asyncio.to_thread est la manière la plus simple de déporter un appel bloquant : elle exécute la fonction dans le pool de threads par défaut de la boucle et rend une coroutine que l'on attend comme n'importe quelle autre. Les variables de contexte sont copiées dans le thread, ce qui préserve par exemple l'identifiant de corrélation des journaux.

```python
import asyncio

def lire_rapport(chemin: str) -> str:
    with open(chemin, encoding="utf-8") as fichier:
        return fichier.read()

async def traiter(chemin: str) -> int:
    contenu = await asyncio.to_thread(lire_rapport, chemin)
    return len(contenu)
```

Le pool de threads par défaut a une taille limitée, calculée à partir du nombre de processeurs. Si l'application déporte beaucoup d'appels lents en même temps, les appels supplémentaires attendent qu'un thread se libère : la boucle reste réactive, mais le débit des appels bloquants plafonne. On peut alors fournir son propre exécuteur avec une taille adaptée et le déclarer comme exécuteur par défaut de la boucle avec loop.set_default_executor.

Un thread ne règle pas le cas d'un calcul intensif écrit en Python pur, à cause du GIL : le calcul ne s'exécute pas en parallèle de la boucle, il lui dispute le même verrou. Pour ce cas, on confie le travail à un exécuteur de processus. La méthode loop.run_in_executor accepte n'importe quel exécuteur de concurrent.futures, dont ProcessPoolExecutor, et rend un futur que la coroutine attend sans bloquer la boucle.

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

def calculer_empreinte(donnees: bytes) -> str:
    import hashlib
    return hashlib.sha256(donnees).hexdigest()

async def empreintes(lots: list[bytes]) -> list[str]:
    boucle = asyncio.get_running_loop()
    with ProcessPoolExecutor(max_workers=4) as executeur:
        futurs = [boucle.run_in_executor(executeur, calculer_empreinte, lot) for lot in lots]
        return await asyncio.gather(*futurs)
```

Un exécuteur de processus impose ses propres contraintes. Les arguments et le résultat traversent une frontière de processus : ils doivent être sérialisables par pickle, et leur copie a un coût qui peut dépasser le gain pour de petites tâches. La fonction exécutée doit être définie au niveau d'un module, jamais comme une fonction locale ou une lambda, sinon pickle ne sait pas la retrouver dans le processus enfant. Enfin, créer un pool de processus coûte cher : on le crée une fois au démarrage de l'application et on le réutilise plutôt que d'en ouvrir un par requête.

L'annulation mérite une attention particulière. Annuler la coroutine qui attend asyncio.to_thread ou run_in_executor lève CancelledError dans la coroutine, mais n'interrompt pas le thread ou le processus qui exécute la fonction : celle-ci va à son terme et son résultat est simplement abandonné. Une fonction déportée qui écrit dans une base ou envoie un message doit donc être idempotente, ou vérifier elle-même un indicateur d'arrêt à intervalles réguliers.

Pour détecter les appels bloquants oubliés, on active le mode debug de la boucle, qui journalise tout callback ou toute étape de coroutine qui dépasse le seuil slow_callback_duration, cent millisecondes par défaut. Le message indique la coroutine fautive, ce qui suffit en général à retrouver l'appel synchrone qu'il faut déporter ou remplacer par son équivalent asynchrone.

## Processus et multiprocessing

Le module multiprocessing lance de vrais processus, chacun avec son propre interpréteur et son propre GIL : c'est la manière d'utiliser plusieurs cœurs pour du calcul en Python pur. La méthode de démarrage spawn, par défaut sur macOS et Windows, relance un interpréteur neuf et réimporte le module principal, d'où la garde if __name__ == "__main__" obligatoire autour du code de démarrage.
