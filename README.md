# segui_bus

Notifica quando un bus SETA è abbastanza vicino.

Fonte dati: https://proxy.busone.app/


## Obiettivo

L'utente vuole essere avvisato quando l'autobus (o un generico autobus di una linea) è vicino alla fermata selezionata. la selezione del bus può avvenire per numero di mezzo o linea. la fermata per numero oppure coordinate geografiche. Il lavoro si articola così:

1. **Recuperare i dati e filtrare un veicolo specifico** — interrogare la
   fonte dati e isolare un singolo
   mezzo tramite il suo numero identificativo.
2. **Risalire al numero di una fermata dalla sua posizione**
3. **Risalire al numero del veicolo dal numero di linea** — data la linea (o la corsa), determinare  
   quale/i mezzo/i la sta servendo in quel momento, ovvero quale è quello che più prossimo ad una fermata, così da poter monitorare il bus giusto senza sapere a priori il suo numero.
4. **Confrontare i dati real-time con gli orari previsti** — usare gli orari
   programmati (GTFS statico) per sapere quale corsa aspettare e verificare se il veicolo rilevato è in orario, in ritardo o in anticipo rispetto alla tabella.
5. **allarme prossimità** - alzare un allarme quando
   il mezzo è ad una certa distanza temporale dalla fermata, ovvero quando si trova in un raggio relativo ad una certa posizione (o alla fermata)
6. **strumento di avviso** - usare l'allarme per
   inviare una notifica di avviso (es telegram bot)
7. **integrazione home assistant**  - per applicazioni
   domotiche come avvisi luminosi o per la semplice disponibilità dei dati


## Struttura del codice

oggetti:
- getter dei dati,
- helper ricerca:
- - filtro mezzo (per vari parametri)
- - filtro fermata 
- calcolo puntualità
- allarme prossimità
- bot
- helper home assistant