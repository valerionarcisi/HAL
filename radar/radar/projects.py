"""Cosa cerca ogni film. Fonte: SOUL.md di Hermes, decisioni di Valerio."""

SENDER = "Valerio Narcisi"
SITE = "valerionarcisi.me"

# Rapporti reali con una casa, per dominio del sito. Solo fatti confermati da Valerio.
RELATIONS = {
    "premierefilm.it": "Ha già in distribuzione Walkie Talkie, il prossimo corto di Valerio.",
}

PROJECTS = {
    "non-fa-ridere": {
        "title": "Non fa ridere",
        "need": "distribuzione festival di un cortometraggio, in Italia e all'estero",
        "brief": (
            "Non fa ridere è un cortometraggio di Valerio Narcisi, in montaggio, autoprodotto. "
            "È una commedia in dialetto. L'obiettivo è una strategia festival internazionale "
            "(Clermont-Ferrand, Encounters Bristol, Vienna Shorts, Winterthur e simili). "
            "I sottotitoli inglesi sono curati come traduzione letteraria."
        ),
        "rubric": {
            "attiva": "attività visibile negli ultimi 3 anni (0-20)",
            "corti": "distribuisce o vende cortometraggi (0-20)",
            "festival": "titoli in festival di corti rilevanti (0-20)",
            "genere": "adatta a una commedia d'autore, non solo animazione o sperimentale (0-15)",
            "apertura": "accetta proposte esterne o titoli italiani (0-10)",
            "contatto": "email diretta e verificata (0-15)",
        },
        "rules": "Cerchiamo distributori e sales agent di corti, non produttori. Il film è in montaggio: non dire che è finito o pronto.",
    },
    "walkie-talkie": {
        "title": "Walkie Talkie",
        "need": "sviluppo, produzione o coproduzione europea",
        "brief": (
            "Walkie Talkie è il prossimo progetto di Valerio Narcisi, in pre-produzione (soggetto v1.2). "
            "Genere: dramma familiare. Quando la figlia si perde, il secondo atto ha la tensione di un "
            "thriller domestico, ma si presenta come dramma familiare. Premessa universale, poco "
            "legata al dialetto: è il progetto per lab e coproduzioni europee."
        ),
        "rubric": {
            "attiva": "attività visibile negli ultimi 3 anni (0-20)",
            "esordienti": "produce corti o opere prime di registi emergenti (0-25)",
            "genere": "dramma d'autore, famiglia, tensione domestica (0-20)",
            "coproduzione": "coproduzioni europee o con l'Italia (0-15)",
            "scala": "struttura alla portata di un esordiente, non una major (0-10)",
            "contatto": "email diretta e verificata (0-10)",
        },
        "rules": (
            "Walkie Talkie ha già distribuzione (Prem1ere Film): NON cercare distributori e NON "
            "nominare la distribuzione nelle mail."
        ),
    },
}
