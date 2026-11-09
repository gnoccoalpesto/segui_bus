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


## Comandi Telegram

Il bot accetta comandi dai preset solo dalla chat configurata in
`SEGUI_BUS_TELEGRAM_CHAT_ID`; ogni altro messaggio viene registrato e ignorato.
La ricezione usa `getUpdates` con long polling e offset, senza webhook.

```
/help                          mostra la sintassi
/list                          elenca i preset (* = seguita)
/watch <id> | /watch_<id>      segue un preset e ne mostra i valori effettivi
/add                           risponde con il modello da compilare
/add <id> chiave=valore ...    crea un preset
/set <id> chiave=valore ...    modifica un preset
/remove <id>                   elimina un preset
```

`/add` senza argomenti risponde con questo modello (ordine fisso):

```
line=
stop=
radius_m=150
stops_n=auto
lead_minutes=
earliest=
on_time_tolerance_s=240
assumed_speed_kmh=auto
route_detour_factor=auto
position_mismatch_m=auto
strategy=auto
```

- I comandi e gli id non distinguono maiuscole/minuscole; gli id vengono
  normalizzati in minuscolo (`[a-z0-9_-]+`). Le chiavi `chiave=valore` sono
  quelle minuscole del modello qui sopra.
- I valori con spazi si racchiudono tra virgolette, ad esempio
  `stop="via roma"`. Tutto ciò che segue un `#` non quotato viene ignorato,
  quindi una risposta con i valori effettivi (ad esempio
  `stops_n=auto  # 1`) si può incollare tal quale in `/set`.
- Le modifiche sono validate dal preset store prima di toccare la memoria e poi
  riscritte in modo atomico in `config.json`. Un comando non valido risponde con
  il motivo e la sintassi e non cambia nulla.
- Seguire un preset azzera lo stato d'allarme del nuovo watch. Un `/set` che
  cambia solo soglie, strategia o `earliest` conserva lo stato; un cambio di
  linea o di fermata risolta, oppure `/remove`, lo scarta.
- I campi `auto` non vengono mai salvati: sono ricalcolati dal feed statico e
  mostrati con il valore calcolato accanto.

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