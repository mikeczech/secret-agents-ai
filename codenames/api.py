from typing import Optional, List
import jsonpickle
from fastapi import FastAPI, Depends, Cookie, Request, HTTPException, Form
from sqlalchemy.orm import Session
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
import asyncio
import spacy
import numpy as np

from codenames import models, schemas
from codenames.sql import SQLAlchemyGameManager, SQLAlchemyGameBackend
from codenames.game import (
    Game,
    Color,
    Role,
    RoleOccupiedException,
    AlreadyJoinedException,
    GameAlreadyExistsException,
    AuthorizationException,
    InvalidColorRoleCombination,
    StateException,
)
from codenames.database import SessionLocal, engine

models.Base.metadata.create_all(bind=engine)

app = FastAPI()

MESSAGE_STREAM_DELAY = 1  # second
MESSAGE_STREAM_RETRY_TIMEOUT = 15000  # milisecond

# add CORS so our web page can connect to our api
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_game_manager():
    db = SessionLocal()
    manager = SQLAlchemyGameManager(db)
    try:
        yield manager
    finally:
        db.close()


def get_game_backend(game_id: int):
    db = SessionLocal()
    backend = SQLAlchemyGameBackend(game_id, db)
    try:
        yield backend
    finally:
        db.close()

nlp = spacy.load("en_vectors_floret_lg")

def get_nlp():
    return nlp

@app.get("/games/{game_id}/words")
def read_active_words(backend: SQLAlchemyGameBackend = Depends(get_game_backend)):
    return [
        {
            "color": w.color,
            "word": w.word.value,
            "id": w.id,
            "is_active": False if w.move else True,
        }
        for w in backend.read_active_words()
    ]


@app.get("/games/{game_id}/hints", response_model=List[schemas.Hint])
def read_hints(backend: SQLAlchemyGameBackend = Depends(get_game_backend)):
    return backend.read_hints()


@app.get("/games/{game_id}/players", response_model=List[schemas.Player])
def read_players(backend: SQLAlchemyGameBackend = Depends(get_game_backend)):
    return backend.read_players()


@app.get("/games/{game_id}/conditions", response_model=List[schemas.Condition])
def read_conditions(backend: SQLAlchemyGameBackend = Depends(get_game_backend)):
    return backend.read_conditions()


@app.put("/games/{game_id}/join")
def join_game(
    player: schemas.PlayerCreate,
    session_id: Optional[str] = Cookie(None),
    backend: SQLAlchemyGameBackend = Depends(get_game_backend),
):
    if session_id is None:
        raise HTTPException(status_code=401, detail="Could not determine session id")
    current_game_state = Game(session_id, backend).load_state()

    ai1_game_state = Game(session_id + "-ai1", backend).load_state()
    ai2_game_state = Game(session_id + "-ai2", backend).load_state()
    ai3_game_state = Game(session_id + "-ai3", backend).load_state()

    try:
        current_game_state.join(
            Color(player.color_id), Role(player.role_id), player.name
        )

        ai1_game_state.join(
            Color(player.color_id).toggle(), Role(player.role_id), "Ng (AI)"
        )

        ai2_game_state.join(
            Color(player.color_id), Role(player.role_id).toggle(), "LeCun (AI)"
        )

        ai3_game_state.join(
            Color(player.color_id).toggle(), Role(player.role_id).toggle(), "Hinton (AI)"
        )
    except RoleOccupiedException as ex:
        raise HTTPException(
            status_code=403,
            detail="This color and role is already occupied by another player",
        )
    except AlreadyJoinedException as ex:
        raise HTTPException(
            status_code=403, detail="This user has already joined the game"
        )
    except InvalidColorRoleCombination as ex:
        raise HTTPException(
            status_code=403,
            detail=f"Invalid color / role combination: color = {player.color_id}, role = {player.role_id}",
        )
    except StateException as ex:
        raise HTTPException(
            status_code=400,
            detail=ex.message,
        )
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Cannot join the game (maybe the game is already running)",
        )
    return {
        "message": f"Successfully joined the game {backend.game_id} with color {player.color_id} and role {player.role_id}."
    }


@app.put("/games/{game_id}/start")
def start_game(
    session_id: Optional[str] = Cookie(None),
    backend: SQLAlchemyGameBackend = Depends(get_game_backend),
):
    if session_id is None:
        raise HTTPException(status_code=401, detail="Could not determine session id")
    current_game_state = Game(session_id, backend).load_state()
    try:
        current_game_state.start_game()
    except StateException as ex:
        raise HTTPException(status_code=403, detail=ex.message)
    except Exception as ex:
        raise HTTPException(status_code=400, detail="Cannot start the game")

    return {"message": "Successfully started the game"}


@app.put("/games/{game_id}/give_hint")
def give_hint(
    hint: schemas.HintCreate,
    session_id: Optional[str] = Cookie(None),
    backend: SQLAlchemyGameBackend = Depends(get_game_backend),
):
    if session_id is None:
        raise HTTPException(status_code=401, detail="Could not determine session id")
    current_game_state = Game(session_id, backend).load_state()
    try:
        current_game_state.give_hint(hint.word, hint.num)
    except AuthorizationException as ex:
        raise HTTPException(status_code=401, detail=ex.message)
    except StateException as ex:
        raise HTTPException(status_code=403, detail=ex.message)
    except Exception as ex:
        raise HTTPException(status_code=400, detail="Cannot give a hint")

    return {
        "message": f"Successfully given the hint '{hint.word}' with num = {hint.num}"
    }


@app.put("/games/{game_id}/end_turn")
def end_turn(
    session_id: Optional[str] = Cookie(None),
    backend: SQLAlchemyGameBackend = Depends(get_game_backend),
):
    if session_id is None:
        raise HTTPException(status_code=401, detail="Could not determine session id")
    current_game_state = Game(session_id, backend).load_state()
    try:
        current_game_state.end_turn()
    except AuthorizationException as ex:
        raise HTTPException(status_code=401, detail=ex.message)
    except StateException as ex:
        raise HTTPException(status_code=403, detail=ex.message)
    except Exception as ex:
        raise HTTPException(status_code=400, detail="Cannot end turn")

    return {"message": f"Successfully ended the turn"}


@app.get("/games/{game_id}/similarity")
def similarity(
    hint: str,
    backend: SQLAlchemyGameBackend = Depends(get_game_backend),
    nlp = Depends(get_nlp)
):
    active_words = backend.read_active_words()
    hint_word = nlp.vocab[hint.lower()]

    ids = [w.id for w in active_words]
    if hint == "":
        return dict(zip(ids, [1.0] * len(ids)))
    sim = np.array([hint_word.similarity(nlp.vocab[w.word.value.lower()]) for w in active_words])
    norm = (sim - sim.min()) / (sim.max() - sim.min())
    return dict(zip(ids, norm))


@app.put("/games/{game_id}/guess")
def guess(
    guess: schemas.GuessCreate,
    session_id: Optional[str] = Cookie(None),
    backend: SQLAlchemyGameBackend = Depends(get_game_backend),
):
    if session_id is None:
        raise HTTPException(status_code=401, detail="Could not determine session id")
    current_game_state = Game(session_id, backend).load_state()
    try:
        current_game_state.guess(guess.word_id)
    except AuthorizationException as ex:
        raise HTTPException(status_code=401, detail=ex.message)
    except StateException as ex:
        raise HTTPException(status_code=403, detail=ex.message)
    except Exception as ex:
        raise HTTPException(status_code=400, detail="Cannot give a hint")

    return {"message": f"Successfully guessed word '{guess.word_id}'"}


@app.post("/games/")
def create_game(
    game: schemas.GameCreate,
    session_id: Optional[str] = Cookie(None),
    game_manager: SQLAlchemyGameManager = Depends(get_game_manager),
):
    try:
        result = game_manager.create_random(game.name, session_id, random_seed=66)
    except GameAlreadyExistsException as ex:
        raise HTTPException(
            status_code=403, detail=f"The game {game.name} already exists"
        )
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Could not create the game: {ex}")

    return {
        "message": f"Successfully created the game '{game.name}'.",
        "game_id": result.id,
    }


@app.get("/updates/{game_id}")
async def message_stream(
    request: Request, backend: SQLAlchemyGameBackend = Depends(get_game_backend)
):
    async def get_game_info():
        return backend.load()

    async def event_generator():
        while True:
            if await request.is_disconnected():
                logger.debug("Request disconnected")
                break

            game_info = await get_game_info()
            yield {
                "event": "new_message",
                "id": "message_id",
                "retry": MESSAGE_STREAM_RETRY_TIMEOUT,
                "data": jsonpickle.encode(game_info, unpicklable=False),
            }

            await asyncio.sleep(MESSAGE_STREAM_DELAY)

    return EventSourceResponse(event_generator())
