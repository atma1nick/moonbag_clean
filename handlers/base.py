from database import async_session
from models import User


async def get_user(user_id: int) -> User | None:
    async with async_session() as s:
        return await s.get(User, user_id)


async def ensure_user(user_id: int, username: str = None) -> User:
    async with async_session() as s:
        user = await s.get(User, user_id)
        if not user:
            user = User(user_id=user_id, username=username)
            s.add(user)
            await s.commit()
            await s.refresh(user)
        elif username and user.username != username:
            user.username = username
            await s.commit()
    return user
