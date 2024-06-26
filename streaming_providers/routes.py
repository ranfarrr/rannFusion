import asyncio
import logging

from fastapi import (
    Request,
    Response,
    HTTPException,
    APIRouter,
)
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis

from db import crud
from db.config import settings
from streaming_providers import mapper
from streaming_providers.alldebrid.utils import get_direct_link_from_alldebrid
from streaming_providers.debridlink.api import router as debridlink_router
from streaming_providers.debridlink.utils import get_direct_link_from_debridlink
from streaming_providers.exceptions import ProviderException
from streaming_providers.offcloud.utils import get_direct_link_from_offcloud
from streaming_providers.pikpak.utils import get_direct_link_from_pikpak
from streaming_providers.premiumize.api import router as premiumize_router
from streaming_providers.premiumize.utils import get_direct_link_from_premiumize
from streaming_providers.realdebrid.api import router as realdebrid_router
from streaming_providers.realdebrid.utils import get_direct_link_from_realdebrid
from streaming_providers.seedr.api import router as seedr_router
from streaming_providers.seedr.utils import get_direct_link_from_seedr
from streaming_providers.torbox.utils import get_direct_link_from_torbox
from streaming_providers.qbittorrent.utils import get_direct_link_from_qbittorrent
from utils import crypto, torrent, wrappers, const
from utils.lock import acquire_redis_lock, release_redis_lock
from utils.network import get_client_ip

router = APIRouter()


async def get_cached_stream_url(redis: Redis, cached_stream_url_key):
    if cached_stream_url := await redis.get(cached_stream_url_key):
        cached_stream_url = cached_stream_url.decode("utf-8")
        return cached_stream_url
    return None


@router.get("/{secret_str}/stream", tags=["streaming_provider"])
@wrappers.exclude_rate_limit
@wrappers.auth_required
async def streaming_provider_endpoint(
    secret_str: str,
    info_hash: str,
    response: Response,
    request: Request,
    season: int = None,
    episode: int = None,
):
    response.headers.update(const.NO_CACHE_HEADERS)

    user_data = request.scope.get("user", crypto.decrypt_user_data(secret_str))
    if not user_data.streaming_provider:
        raise HTTPException(status_code=400, detail="No streaming provider set.")

    stream = await crud.get_stream_by_info_hash(info_hash)
    if not stream:
        raise HTTPException(status_code=400, detail="Stream not found.")

    magnet_link = await torrent.convert_info_hash_to_magnet(
        info_hash, stream.announce_list
    )

    episode_data = stream.get_episode(season, episode)
    filename = episode_data.filename if episode_data else stream.filename
    user_ip = get_client_ip(request)
    redirect_status_code = 302
    cached_stream_url_key = "streaming_provider_" + crypto.get_text_hash(
        f"{user_ip}_{secret_str}_{info_hash}_{filename}_{stream.file_index}",
        full_hash=True,
    )

    if cached_stream_url := await get_cached_stream_url(
        request.app.state.redis, cached_stream_url_key
    ):
        return RedirectResponse(
            url=cached_stream_url,
            headers=response.headers,
            status_code=redirect_status_code,
        )

    # create a redis lock to prevent multiple requests from initiating a download task.
    acquired, lock = await acquire_redis_lock(
        request.app.state.redis,
        f"{cached_stream_url_key}_locked",
        timeout=60,
    )
    if not acquired:
        raise HTTPException(status_code=429, detail="Too many requests.")

    try:
        if user_data.streaming_provider.service == "seedr":
            video_url = await get_direct_link_from_seedr(
                info_hash, magnet_link, user_data, stream, filename, 1, 0
            )
        elif user_data.streaming_provider.service == "realdebrid":
            video_url = get_direct_link_from_realdebrid(
                info_hash,
                magnet_link,
                user_data,
                filename,
                stream.file_index,
                1,
                0,
                user_ip=user_ip,
                episode=episode,
            )
        elif user_data.streaming_provider.service == "alldebrid":
            video_url = get_direct_link_from_alldebrid(
                info_hash, magnet_link, user_data, filename, 1, 0
            )
        elif user_data.streaming_provider.service == "offcloud":
            video_url = get_direct_link_from_offcloud(
                info_hash, magnet_link, user_data, filename, 1, 0
            )
        elif user_data.streaming_provider.service == "pikpak":
            video_url = await get_direct_link_from_pikpak(
                info_hash, magnet_link, user_data, stream, filename, 1, 0
            )
        elif user_data.streaming_provider.service == "torbox":
            video_url = get_direct_link_from_torbox(
                info_hash, magnet_link, user_data, filename, 1, 0
            )
        elif user_data.streaming_provider.service == "premiumize":
            video_url = get_direct_link_from_premiumize(
                info_hash, magnet_link, user_data, stream.torrent_name, filename, 1, 0
            )
        elif user_data.streaming_provider.service == "qbittorrent":
            video_url = await get_direct_link_from_qbittorrent(
                info_hash, magnet_link, user_data, stream, filename, 1, 0
            )
        else:
            video_url = get_direct_link_from_debridlink(
                info_hash, magnet_link, user_data, filename, stream.file_index, 1, 0
            )

        # Cache the streaming URL for 1 hour & release the lock
        await request.app.state.redis.set(
            cached_stream_url_key, video_url.encode("utf-8"), ex=3600
        )
        await release_redis_lock(lock)

    except ProviderException as error:
        logging.error(
            "Exception occurred for %s: %s",
            info_hash,
            error.message,
            exc_info=True if error.video_file_name == "api_error.mp4" else False,
        )
        video_url = f"{settings.host_url}/static/exceptions/{error.video_file_name}"
        redirect_status_code = 307
    except Exception as e:
        logging.error("Exception occurred for %s: %s", info_hash, e, exc_info=True)
        video_url = f"{settings.host_url}/static/exceptions/api_error.mp4"
        redirect_status_code = 307

    return RedirectResponse(
        url=video_url, headers=response.headers, status_code=redirect_status_code
    )


@router.get("/{secret_str}/delete_all_watchlist", tags=["streaming_provider"])
@wrappers.exclude_rate_limit
@wrappers.auth_required
async def delete_all_watchlist(request: Request, response: Response, secret_str: str):
    response.headers.update(const.NO_CACHE_HEADERS)

    user_data = request.scope.get("user", crypto.decrypt_user_data(secret_str))

    if not user_data.streaming_provider:
        raise HTTPException(status_code=400, detail="No streaming provider set.")

    if delete_all_watchlist_function := mapper.DELETE_ALL_WATCHLIST_FUNCTIONS.get(
        user_data.streaming_provider.service
    ):
        try:
            if asyncio.iscoroutinefunction(delete_all_watchlist_function):
                await delete_all_watchlist_function(user_data)
            else:
                delete_all_watchlist_function(user_data)
            video_url = f"{settings.host_url}/static/exceptions/watchlist_deleted.mp4"
        except ProviderException as error:
            logging.error(
                "Exception occurred while deleting watchlist: %s",
                error.message,
                exc_info=True,
            )
            video_url = f"{settings.host_url}/static/exceptions/{error.video_file_name}"
        except Exception as e:
            logging.error(
                "Exception occurred while deleting watchlist: %s", e, exc_info=True
            )
            video_url = f"{settings.host_url}/static/exceptions/api_error.mp4"
    else:
        raise HTTPException(
            status_code=400, detail="Provider does not support this action."
        )

    return RedirectResponse(url=video_url, headers=response.headers)


router.include_router(seedr_router, prefix="/seedr", tags=["seedr"])
router.include_router(realdebrid_router, prefix="/realdebrid", tags=["realdebrid"])
router.include_router(debridlink_router, prefix="/debridlink", tags=["debridlink"])
router.include_router(premiumize_router, prefix="/premiumize", tags=["premiumize"])
