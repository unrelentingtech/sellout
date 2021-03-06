import os
import re
import typing
import functools
import mimetypes
import tomlkit
from typing import Any, Tuple, List, Iterable, Mapping, TypedDict
from datetime import datetime, timedelta, timezone
from hashlib import sha1, sha256
from base64 import urlsafe_b64decode, b64encode
from secrets import token_urlsafe
from urllib.parse import urlencode
from rfc3986 import uri_reference
from dotenv import load_dotenv
from argon2 import PasswordHasher, exceptions as argonerr
from cryptography.hazmat.primitives import constant_time
from multipart.multipart import parse_options_header
from pydantic import BaseModel, ValidationError, conlist
from slugify import slugify
from starlette.applications import Starlette
from starlette.requests import Request, HTTPConnection
from starlette.responses import Response, RedirectResponse, JSONResponse
from starlette.endpoints import HTTPEndpoint
from starlette.authentication import (
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
    SimpleUser,
    UnauthenticatedUser,
    AuthCredentials,
    has_required_scope,
)
from starlette.routing import Route, Mount
from starlette.exceptions import HTTPException
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.datastructures import Headers, MutableHeaders, FormData, UploadFile
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from aiodynamo.client import Client as DbClient
from aiodynamo.credentials import Credentials as DbCreds
from aiodynamo.errors import ItemNotFound
from aiodynamo.http.httpx import HTTPX
from httpx_auth import AWS4Auth
from gidgethub.httpx import GitHubAPI
from httpx import AsyncClient

SCOPE_INFO = {
    "profile": "Get basic profile information",
    "email": "Get profile email address",
    "create": "Create new posts using Micropub",
    "update": "Edit existing posts using Micropub",
    "delete": "Delete posts using Micropub",
    "undelete": "Restore deleted posts using Micropub",
    "media": "Upload files using Micropub",
}
ALL_SCOPES = [k for k in SCOPE_INFO.keys()]
CSP_NOSCRIPT = "default-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'none'; script-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
COMMON_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",  # kinda redundant with CSP but why not
    "Permissions-Policy": "sync-xhr=(), accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
}
DEFAULT_HEADERS = {**COMMON_HEADERS, "Content-Security-Policy": CSP_NOSCRIPT}
FRONTMATTER_RE = re.compile(r"^\+{3,}\s*$", re.MULTILINE)

load_dotenv()
aws_region = os.environ["AWS_REGION"]
db_prefix = os.environ["DYNAMO_PREFIX"]
media_bucket = os.environ["MEDIA_BUCKET"]
media_prefix = os.environ.get("MEDIA_PREFIX", "")
media_url = os.environ["MEDIA_URL"]
session_secret = os.environ["SESSION_SECRET"]
admin_pw_hash = os.environ["PASSWORD_HASH"]
github_token = os.environ["GITHUB_TOKEN"]
github_owner, github_repo = os.environ["GITHUB_REPO"].split("/")
github_branch = os.environ["GITHUB_BRANCH"]
path_prefix = os.environ.get("PATH_PREFIX", "content/")
hasher = PasswordHasher()
tpl = Jinja2Templates(directory="tpl")

mimetypes.add_type("font/woff2", ".woff2")


class DataError(Exception):
    pass


def db_table(h, tbl):
    return DbClient(HTTPX(h), DbCreds.auto(), aws_region).table(db_prefix + tbl)


# CloudFront -> API Gateway problems :/
class WeirdnessMiddleware(object):
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = MutableHeaders(scope=scope)
            xhost = headers.get("x-forwarded-host")
            xauth = headers.get("x-authorization")
            if xhost:
                headers["host"] = xhost
            if xauth:
                headers["authorization"] = xauth
        await self.app(scope, receive, send)


# https://github.com/encode/starlette/pull/920
def requires(
    scopes: typing.Union[str, typing.Sequence[str]],
    redirect: str = None,
) -> typing.Callable:
    scopes_list = [scopes] if isinstance(scopes, str) else list(scopes)

    def decorator(func: typing.Callable) -> typing.Callable:
        @functools.wraps(func)
        async def async_wrapper(*args: typing.Any, **kwargs: typing.Any) -> Response:
            request = kwargs.get("request", args[-1] if args else None)
            assert isinstance(request, Request)

            if redirect is None and not has_required_scope(request, ["auth"]):
                raise AuthenticationError(401, {"error": "unauthorized"})
            if not has_required_scope(request, scopes_list):
                if redirect is not None:
                    next_url = "{redirect_path}?{orig_request}".format(
                        redirect_path=request.url_for(redirect),
                        orig_request=urlencode({"next": str(request.url)}),
                    )
                    return RedirectResponse(url=next_url, status_code=303)
                raise AuthenticationError(403, {"error": "insufficient_scope"})
            return await func(*args, **kwargs)

        return async_wrapper

    return decorator


TOK_ERR = AuthenticationError(
    401, {"error": "unauthorized", "error_description": "Token is not valid"}
)


async def authenticate_bearer(
    conn: HTTPConnection, token: str
) -> Tuple[AuthCredentials, BaseUser]:
    async with AsyncClient() as h:
        try:
            data = await db_table(h, "auth").get_item({"token": "B-" + token})
            if data.get("revoked") or data["host"] != conn.headers["host"]:
                raise TOK_ERR
            conn.scope["bearer_data"] = data
            return AuthCredentials(["auth", "via_bearer", *data["scopes"]]), SimpleUser(
                "admin"
            )
        except ItemNotFound:
            raise TOK_ERR


class TokenAndSessionBackend(AuthenticationBackend):
    async def authenticate(
        self, conn: HTTPConnection
    ) -> Tuple[AuthCredentials, BaseUser]:
        if conn.session.get("au", False):
            return AuthCredentials(["auth", "via_cookie", *ALL_SCOPES]), SimpleUser(
                "admin"
            )
        if "Authorization" in conn.headers:
            try:
                scheme, token = conn.headers["Authorization"].split()
                if scheme != "Bearer":
                    raise AuthenticationError(
                        400,
                        {
                            "error": "invalid_request",
                            "error_description": "Unsupported Authorization header scheme {}".format(
                                scheme
                            ),
                        },
                    )
                return await authenticate_bearer(conn, token)
            except ValueError:
                raise AuthenticationError(
                    400,
                    {
                        "error": "invalid_request",
                        "error_description": "What even is this Authorization header?",
                    },
                )


class Login(HTTPEndpoint):
    async def get(self, request: Request) -> Response:
        next = request.query_params.get("next", "/")
        if request.user.is_authenticated:
            return RedirectResponse(url=next, status_code=303)
        return tpl.TemplateResponse(
            "login.html",
            {"next": next, "request": request},
            headers=DEFAULT_HEADERS,
        )

    async def post(self, request: Request) -> Response:
        form = await request.form()
        next = form.get("next", "/.sellout/")
        if request.user.is_authenticated:
            return RedirectResponse(url=next, status_code=303)
        error = "Something error??"
        try:
            if hasher.verify(admin_pw_hash, form.get("pw", "")):
                request.session["au"] = True
                return RedirectResponse(url=next, status_code=303)
        except argonerr.VerifyMismatchError as err:
            print(err)
            error = "The password did not match"
        except argonerr.VerificationError as err:
            print(err)
            error = "Something went wrong with the password check"
        return tpl.TemplateResponse(
            "login.html",
            {"next": next, "error": error, "request": request},
            headers=DEFAULT_HEADERS,
        )


def profile(request: Request) -> Mapping[str, Any]:
    # TODO: actual profile
    return {"me": "https://{}/".format(request.headers["host"])}


async def redeem_auth_code(request: Request, form: FormData) -> Mapping[str, Any]:
    if form.get("grant_type") != "authorization_code":
        raise AuthenticationError(400, {"error": "unsupported_grant_type"})
    if not "code" in form or not "client_id" in form or not "redirect_uri" in form:
        raise AuthenticationError(400, {"error": "invalid_request"})
    async with AsyncClient() as h:
        try:
            tbl = db_table(h, "auth")
            data = await tbl.get_item({"token": "C-" + form["code"]})
            time = datetime.fromisoformat(data["time"])
            if (
                datetime.utcnow() - time > timedelta(minutes=5)
                or form["client_id"] != data["client_id"]
                or form["redirect_uri"] != data["redirect_uri"]
                or data.get("used", False)
                or data["host"] != request.headers["host"]
            ):
                raise AuthenticationError(400, {"error": "invalid_grant"})
            if data.get("code_challenge_method") == "S256":
                if not "code_verifier" in form:
                    raise AuthenticationError(400, {"error": "invalid_request"})
                if not constant_time.bytes_eq(
                    sha256(form["code_verifier"].encode("ascii")).digest(),
                    urlsafe_b64decode(data["code_challenge"] + "=="),
                ):
                    # ^^ fun fact, we can always just add the padding: https://stackoverflow.com/a/49459036
                    raise AuthenticationError(400, {"error": "invalid_grant"})
            data["used"] = True
            await tbl.put_item(data)
            return data
        except (ItemNotFound, KeyError):
            raise AuthenticationError(400, {"error": "invalid_grant"})


def autherr(request: Request, err) -> Response:
    return tpl.TemplateResponse(
        "autherr.html",
        {
            "request": request,
            "err": err,
        },
        status_code=400,
        headers=DEFAULT_HEADERS,
    )


class Authorization(HTTPEndpoint):
    @requires("via_cookie", redirect="login")
    async def get(self, request: Request) -> Response:
        if request.query_params.get("response_type") != "code":
            return autherr(request, "response_type MUST be 'code'")
        if not "client_id" in request.query_params:
            return autherr(request, "client_id MUST exist")
        if not "redirect_uri" in request.query_params:
            return autherr(request, "redirect_uri MUST exist")
        if not "state" in request.query_params:
            return autherr(request, "state MUST exist")
        client_id = uri_reference(request.query_params.get("client_id")).normalize()
        if not client_id.is_valid(require_scheme=True, require_authority=True):
            return autherr(request, "client_id MUST be a valid URL")
        redirect_uri = uri_reference(
            request.query_params.get("redirect_uri")
        ).normalize()
        if not redirect_uri.is_valid(require_scheme=True, require_authority=True):
            return autherr(request, "redirect_uri MUST be a valid URL")
        if (
            client_id.scheme != redirect_uri.scheme
            or client_id.authority != redirect_uri.authority
        ):
            # TODO allow things linked by rel=redirect_uri
            return autherr(
                request,
                "redirect_uri MUST be on the same host as client_id (TODO: or an allowed one)",
            )
        req_scopes = request.query_params.get("scope", "profile").split()
        return tpl.TemplateResponse(
            "authorize.html",
            {
                "scope_info": SCOPE_INFO,
                "req_scopes": req_scopes,
                "request": request,
            },
            headers=DEFAULT_HEADERS,
        )

    async def post(self, request: Request) -> Response:
        form = await request.form()
        await redeem_auth_code(request, form)
        return JSONResponse(profile(request))


class Token(HTTPEndpoint):
    @requires("via_bearer", redirect="login")
    async def get(self, request: Request) -> Response:
        resp = profile(request)
        bd = request.scope["bearer_data"]
        resp["client_id"] = bd["client_id"]
        resp["scope"] = " ".join(bd["scopes"])
        return JSONResponse(resp)

    async def post(self, request: Request) -> Response:
        form = await request.form()
        if form.get("action") == "revoke":
            async with AsyncClient() as h:
                try:
                    tbl = db_table(h, "auth")
                    data = await tbl.get_item({"token": "B-" + form["token"]})
                    if data["host"] == request.headers["host"]:
                        data["revoked"] = True
                        await tbl.put_item(data)
                except Exception as e:
                    # for requests from the admin UI, do not follow the OAuth spec and return the error
                    if has_required_scope(request, ["via_cookie"]):
                        # XXX: says "indieauth client"
                        return autherr(request, str(e))
            return JSONResponse({})
        code_data = await redeem_auth_code(request, form)
        bearer = token_urlsafe(16)
        data = {
            "token": "B-" + bearer,
            "time": datetime.utcnow().isoformat(),
            "code_used": code_data["token"],
            "client_id": code_data["client_id"],
            "scopes": code_data["scopes"],
            "host": request.headers["host"],
        }
        async with AsyncClient() as h:
            await db_table(h, "auth").put_item(data)
        resp = profile(request)
        resp["token_type"] = "Bearer"
        resp["access_token"] = bearer
        resp["scope"] = " ".join(data["scopes"])
        return JSONResponse(resp)


@requires("via_cookie", redirect="login")
async def allow(request: Request) -> Response:
    if request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
        return autherr(request, "request MUST be same-origin")
    form = await request.form()
    if not "client_id" in form:
        return autherr(request, "client_id MUST exist")
    if not "redirect_uri" in form:
        return autherr(request, "redirect_uri MUST exist")
    if not "state" in form:
        return autherr(request, "state MUST exist")
    scopes = [s for s in SCOPE_INFO.keys() if form.get("scope:" + s) == "on"]
    code = token_urlsafe(16)
    data = {
        "token": "C-" + code,
        "time": datetime.utcnow().isoformat(),
        "client_id": form["client_id"],
        "redirect_uri": form["redirect_uri"],
        "state": form["state"],
        "code_challenge": form.get("code_challenge"),
        "code_challenge_method": form.get("code_challenge_method"),
        "scopes": scopes,
        "host": request.headers["host"],
    }
    redir_uri = uri_reference(form["redirect_uri"]).normalize()
    redir_dest = redir_uri.copy_with(
        query=(redir_uri.query or "")
        + ("" if not redir_uri.query else "&")
        + urlencode({"code": code, "state": form["state"]})
    ).unsplit()
    async with AsyncClient() as h:
        await db_table(h, "auth").put_item(data)
    return RedirectResponse(url=redir_dest, status_code=303)


@requires("via_cookie", redirect="login")
async def logout(request: Request) -> Response:
    del request.session["au"]
    return RedirectResponse(url="/", status_code=303)


@requires("via_cookie", redirect="login")
async def dashboard(request: Request) -> Response:
    return tpl.TemplateResponse(
        "dashboard.html",
        {"request": request},
        headers=DEFAULT_HEADERS,
    )


async def testpage(request: Request) -> Response:
    return tpl.TemplateResponse(
        "testpage.html",
        {"request": request},
        headers={
            "Link": '</.sellout/authz>; rel="authorization_endpoint", </.sellout/token>; rel="token_endpoint", </.sellout/pub>; rel="micropub"',
            **DEFAULT_HEADERS,
        },
    )


Post = Tuple[tomlkit.toml_document.TOMLDocument, str]


async def get_post(h, path: str) -> Tuple[Post, str]:
    raw_text = await GitHubAPI(h, "sellout", oauth_token=github_token).getitem(
        "/repos/{owner}/{repo}/contents/{path}{?ref}",
        url_vars={
            "owner": github_owner,
            "repo": github_repo,
            "path": path,
            "ref": github_branch,
        },
        accept="application/vnd.github.v3.raw",
    )
    _, fm_text, content_text = FRONTMATTER_RE.split(raw_text, 2)
    utf8_text = raw_text.encode("utf-8")
    post_sha = sha1(
        "blob {}\0".format(len(utf8_text)).encode("utf-8") + utf8_text
    ).hexdigest()
    return ((tomlkit.loads(fm_text), content_text), post_sha)


async def put_post(h, path: str, post: Post, post_sha: str = None):
    (fm, content_text) = post
    raw_text = "+++"
    fm_text = tomlkit.dumps(fm)
    if not fm_text.startswith("\n"):
        raw_text += "\n"
    raw_text += fm_text
    if not raw_text.endswith("\n"):
        raw_text += "\n"
    raw_text += "+++\n"
    if not content_text.startswith("\n"):
        raw_text += "\n"
    raw_text += content_text
    data = {
        "branch": github_branch,
        "message": "[micropub] put " + path,
        "content": b64encode(raw_text.encode("utf-8")).decode("ascii"),
    }
    if post_sha:
        data["sha"] = post_sha
    return await GitHubAPI(h, "sellout", oauth_token=github_token).put(
        "/repos/{owner}/{repo}/contents/{path}",
        url_vars={
            "owner": github_owner,
            "repo": github_repo,
            "path": path,
        },
        data=data,
    )


async def delete_post(h, path: str, post_sha: str):
    return await GitHubAPI(h, "sellout", oauth_token=github_token).delete(
        "/repos/{owner}/{repo}/contents/{path}",
        url_vars={
            "owner": github_owner,
            "repo": github_repo,
            "path": path,
        },
        data={
            "branch": github_branch,
            "message": "[micropub] delete " + path,
            "sha": post_sha,
        },
    )


def url2path(request: Request, url: str) -> str:
    parts = uri_reference(url)
    if parts.authority != request.headers["host"] and not request.headers[
        "host"
    ].startswith("127.0.0.1"):
        raise DataError(
            400,
            {
                "error": "invalid_request",
                "error_description": "The provided URL is not on the current domain",
            },
        )
    return os.path.join(path_prefix, parts.path.lstrip("/") + ".md")


async def upload_file(file: UploadFile) -> str:
    # can't stream on Lambda anyway heh
    cont = await file.read()
    assert isinstance(cont, bytes)
    base, ext = os.path.splitext(file.filename)
    name = sha256(cont).hexdigest()[:6] + "_" + slugify(base) + ext
    async with AsyncClient() as h:
        key = await DbCreds.auto().get_key(HTTPX(h))
        assert key != None
        auth = AWS4Auth(
            access_id=key.id,
            secret_key=key.secret,
            security_token=key.token,
            region=aws_region,
            service="s3",
        )
        resp = await h.put(
            "https://{}.s3.amazonaws.com/{}{}".format(media_bucket, media_prefix, name),
            auth=auth,
            headers={
                "Content-Type": file.content_type,
                "Content-Length": str(len(cont)),
                "Content-Disposition": "inline",
                "Cache-Control": "public, max-age=31536000, immutable",
            },
            content=cont,
        )
        print(resp.content)
        if resp.status_code > 300:
            raise DataError(
                500,
                {
                    "error": "server_error",
                    "error_description": "S3 error {}".format(resp.status_code),
                },
            )
    return media_url + name


MfProps = Mapping[str, List[Any]]


class MfObj(BaseModel):
    type: conlist(str, min_items=1)
    properties: MfProps


def post2json(post: Post) -> MfObj:
    (fm, content_text) = post
    props = {}
    for k, v in fm.get("extra", {}).items():
        props[k.replace("_", "-")] = v
    if "title" in fm:
        props["name"] = [fm["title"]]
    if "description" in fm:
        props["summary"] = [fm["description"]]
    if "date" in fm:
        if not isinstance(fm["date"], datetime):
            fm["date"] = datetime.fromisoformat(fm["date"].replace('Z', '+00:00'))
        props["published"] = [fm["date"].isoformat(timespec='seconds')]
    if "updated" in fm:
        if not isinstance(fm["updated"], datetime):
            fm["updated"] = datetime.fromisoformat(fm["updated"].replace('Z', '+00:00'))
        props["updated"] = [fm["updated"].isoformat(timespec='seconds')]
    if "taxonomies" in fm and "tag" in fm["taxonomies"]:
        props["category"] = fm["taxonomies"]["tag"]
    if len(content_text.strip()) > 0:
        props["content"] = [{"markdown": content_text}]
    return MfObj(type=["h-entry"], properties=props)


def json2post_inner(post: Post, props: MfProps, add_mode: bool) -> Post:
    (fm, content_text) = post
    for k, v in props.items():
        if v == None:  # ehhh let's allow nulls why not
            continue
        if len(v) == 0:
            continue
        # TODO: check that these recognized ones are string valued
        if k == "name":
            fm["title"] = v[0]
        elif k == "summary":
            fm["description"] = v[0]
        elif k == "published":
            fm["date"] = datetime.fromisoformat(v[0].replace('Z', '+00:00'))
        elif k == "updated":
            fm["updated"] = datetime.fromisoformat(v[0].replace('Z', '+00:00'))
        elif k == "category":
            if "taxonomies" not in fm:
                fm["taxonomies"] = {}
            if add_mode and "tag" in fm["taxonomies"]:
                fm["taxonomies"]["tag"] += v
            else:
                fm["taxonomies"]["tag"] = v
        elif k == "content":
            # XXX: concatenate if add_mode?? :D
            if isinstance(v[0], str):
                content_text = v[0]
            elif isinstance(v[0], dict):
                content_text = v[0].get(
                    "text", v[0].get("value", v[0].get("markdown", v[0].get("html")))
                )
            if content_text == None:
                raise DataError(
                    400,
                    {
                        "error": "invalid_request",
                        "error_description": "content must be a string or an object with a 'text', 'value', 'markdown' or 'html' key",
                    },
                )
        elif k == "url":
            pass # just don't
        else:
            if "extra" not in fm:
                fm["extra"] = {}
            k = k.replace("-", "_")
            if add_mode and k in fm["extra"]:
                fm["extra"][k] += v
            else:
                fm["extra"][k] = v
    return (fm, content_text)


def json2post(data: MfObj) -> Post:
    fm = tomlkit.toml_document.TOMLDocument()
    content_text = ""
    return json2post_inner((fm, content_text), data.properties, False)


def check_json(data: dict) -> MfObj:
    try:
        return MfObj(**data)
    except ValidationError as e:
        raise DataError(
            400,
            {
                "error": "invalid_request",
                "error_description": str(e),
            },
        )


async def micropub_create(request: Request, data: dict) -> Response:
    category = "notes"
    slug = data.get("mp-slug")
    (fm, content_text) = json2post(check_json(data))
    if not "date" in fm:
        fm["date"] = datetime.now(timezone.utc).replace(microsecond=0)
    if "bearer_data" in request.scope and "client_id" in request.scope["bearer_data"]:
        if not "extra" in fm:
            fm["extra"] = {}
        fm["extra"]["client_id"] = [request.scope["bearer_data"]["client_id"]]
    if "title" in fm:
        category = "articles"
        if not slug:
            slug = slugify(fm["title"])
    elif "extra" in fm and "in_reply_to" in fm["extra"]:
        category = "replies"
    elif "extra" in fm and "like_of" in fm["extra"]:
        category = "likes"
    elif "extra" in fm and "photo" in fm["extra"] and len(content_text.strip()) == 0:
        category = "photos"
    if not slug:
        slug = fm["date"].strftime("%Y-%m-%d-%H-%M-%S")
        fm["slug"] = slug  # store explicitly to prevent Zola from eating the date
    path = category + "/" + slug
    async with AsyncClient() as h:
        await put_post(h, os.path.join(path_prefix, path + ".md"), (fm, content_text))
    return Response(
        None,
        headers={"Location": "https://{}/{}".format(request.headers["host"], path)},
        status_code=201,
    )


def delete_props(post: Post, props: Iterable[str]) -> Post:
    (fm, content_text) = post
    for k in props:
        if k == "name":
            fm.pop("title", None)
        elif k == "published":
            pass  # uhh nope??
        elif k == "updated":
            fm.pop("updated", None)
        elif k == "category":
            if "taxonomies" not in fm:
                continue
            fm["taxonomies"].pop("tag", None)
            if len(fm["taxonomies"]) == 0:
                fm.pop("taxonomies", None)
        elif k == "content":
            content_text = ""
        else:
            if "extra" not in fm:
                continue
            fm["extra"].pop(k.replace("-", "_"), None)
    return (fm, content_text)


def delete_vals(post: Post, props: dict) -> Post:
    (fm, content_text) = post
    for k, v in props.items():
        if not isinstance(v, list):
            raise DataError(
                400,
                {
                    "error": "invalid_request",
                    "error_description": "each property must be a list, check '{}'".format(
                        k
                    ),
                },
            )
        if k in ("name", "published", "updated", "content"):
            pass  # uhh nope?? weird to delete these by value
        elif k == "category":
            if "taxonomies" not in fm or "tag" not in fm["taxonomies"]:
                continue
            fm["taxonomies"]["tag"] = [t for t in fm["taxonomies"]["tag"] if not t in v]
            if len(fm["taxonomies"]["tag"]) == 0:
                fm["taxonomies"].pop("tag", None)
            if len(fm["taxonomies"]) == 0:
                fm.pop("taxonomies", None)
        else:
            k = k.replace("-", "_")
            if "extra" not in fm or k not in fm["extra"]:
                continue
            fm["extra"][k] = [x for x in fm["extra"][k] if not x in v]
            if len(fm["extra"][k]) == 0:
                del fm["extra"]["k"]
    return (fm, content_text)


async def micropub_update(request: Request, data: dict) -> Response:
    if not "url" in data:
        raise DataError(
            400,
            {
                "error": "invalid_request",
                "error_description": "url is required",
            },
        )
    path = url2path(request, data["url"])
    async with AsyncClient() as h:
        (post, sha) = await get_post(h, path)
        if "replace" in data:
            post = json2post_inner(post, data["replace"], False)
        if "add" in data:
            post = json2post_inner(post, data["add"], True)
        if "delete" in data and isinstance(data["delete"], list):
            post = delete_props(post, data["delete"])
        if "delete" in data and isinstance(data["delete"], dict):
            post = delete_vals(post, data["delete"])
        post[0]["updated"] = datetime.now(timezone.utc).replace(microsecond=0)
        await put_post(h, path, post, post_sha=sha)
    return Response(
        None,
        status_code=204,
    )


async def micropub_delete(request: Request, data: dict) -> Response:
    if not "url" in data:
        raise DataError(
            400,
            {
                "error": "invalid_request",
                "error_description": "url is required",
            },
        )
    path = url2path(request, data["url"])
    async with AsyncClient() as h:
        (_, sha) = await get_post(h, path)
        await delete_post(h, path, sha)
    return Response(
        None,
        status_code=204,
    )


class Micropub(HTTPEndpoint):
    @requires("auth")
    async def get(self, request: Request) -> Response:
        q = request.query_params.get("q")
        if q == "config":
            return JSONResponse(
                {
                    "media-endpoint": "https://{}/.sellout/media".format(
                        request.headers["host"]
                    )
                }
            )
        if q == "syndicate-to":
            return JSONResponse({"syndicate-to": []})
        if q == "source":
            url = request.query_params.get("url")
            async with AsyncClient() as h:
                (post, _) = await get_post(h, url2path(request, url))
                return JSONResponse(post2json(post).dict())
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Unsupported ?q value"},
            status_code=400,
        )

    # Cannot use @requires because the token can be in the form >_<
    async def post(self, request: Request) -> Response:
        content_type, options = parse_options_header(
            request.headers.get("content-type")
        )
        if content_type == b"application/json":
            data = await request.json()
            action = data.get("action", "create")
            if not has_required_scope(request, [action]):
                raise AuthenticationError(403, {"error": "insufficient_scope"})
            if action == "create":
                return await micropub_create(request, data)
            if action == "update":
                return await micropub_update(request, data)
            if action == "delete":
                return await micropub_delete(request, data)
            # if action == "undelete":
            # TODO: find last revision with the file in history and restore
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Unsupported action"},
                status_code=400,
            )
        else:
            form = await request.form()
            if "access_token" in form and not "auth" in request.scope:
                (
                    request.scope["auth"],
                    request.scope["user"],
                ) = await authenticate_bearer(request, form["access_token"])
            if not has_required_scope(request, ["create"]):
                raise AuthenticationError(403, {"error": "insufficient_scope"})
            h = "unknown"
            props = {}
            data = {}
            for k, v in form.multi_items():
                if isinstance(v, UploadFile):
                    v = await upload_file(v)
                if k == "h":
                    data["type"] = ["h-" + v]
                elif k == "access_token":
                    pass
                elif k.startswith("mp-"):
                    data[k] = v
                elif k.endswith("[]"):
                    k = k[:-2]
                    if k not in props:
                        props[k] = [v]
                    else:
                        props[k].append(v)
                else:
                    props[k] = [v]
            data["properties"] = props
            return await micropub_create(request, data)
        pass


async def micropub_media(request: Request) -> Response:
    form = await request.form()
    if "access_token" in form and not "auth" in request.scope:
        (
            request.scope["auth"],
            request.scope["user"],
        ) = await authenticate_bearer(request, form["access_token"])
    if not has_required_scope(request, ["media"]):
        raise AuthenticationError(403, {"error": "insufficient_scope"})
    if not "file" in form or not isinstance(form["file"], UploadFile):
        raise DataError(
            400,
            {
                "error": "invalid_request",
                "error_description": "No valid 'file' included in media endpoint request",
            },
        )
    return Response(
        None,
        headers={"Location": await upload_file(form["file"])},
        status_code=201,
    )


def on_auth_error(conn: HTTPConnection, exc: Exception) -> Response:
    (status, obj) = exc.args
    return JSONResponse(obj, status_code=status)


async def on_exception(request: Request, exc: Exception) -> Response:
    (status, obj) = exc.args
    return JSONResponse(obj, status_code=status)


app = Starlette(
    debug=True,
    routes=[
        Route("/", testpage),
        Route("/.sellout/", dashboard),
        Route("/.sellout/pub", Micropub, name="micropub"),
        Route("/.sellout/media", micropub_media, name="media", methods=["POST"]),
        Route("/.sellout/login", Login, name="login"),
        Route("/.sellout/authz", Authorization, name="authz"),
        Route("/.sellout/token", Token, name="token"),
        Route("/.sellout/allow", allow, name="allow", methods=["POST"]),
        Route("/.sellout/logout", logout, name="logout", methods=["POST"]),
        Mount("/.sellout/static", StaticFiles(directory="static"), name="static"),
    ],
    middleware=[
        Middleware(WeirdnessMiddleware),
        Middleware(
            SessionMiddleware,
            secret_key=session_secret,
            session_cookie="__Host-wheeeee",
            same_site="Lax",
            https_only=True,
        ),
        Middleware(
            AuthenticationMiddleware,
            backend=TokenAndSessionBackend(),
            on_error=on_auth_error,
        ),
    ],
    exception_handlers={AuthenticationError: on_exception, DataError: on_exception},
)
