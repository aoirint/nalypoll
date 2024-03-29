from django.shortcuts import render, redirect
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from django.urls import reverse
from django.http import HttpResponseBadRequest, HttpResponseNotAllowed, HttpResponse, Http404
from django.views.decorators.http import require_http_methods

import re
import requests
import requests_oauthlib
from urllib.parse import urlparse, parse_qsl

from tweetutil import TwitterSessionOAuth, TwitterSessionBearer
from validateutil import TWEET_ID_OR_URL_PATTERN
from main.models import *
from main.forms import *

class BadRequestException(Exception):
    def __init__(self, message):
        self.message = message



# Create your views here.
def index(request):
    twitter = TwitterSessionOAuth(request)

    return render(request, 'index.html', {
        'twitter': twitter,
    })


def user(request):
    twitter = TwitterSessionOAuth(request)
    twitter_bearer = TwitterSessionBearer(request)
    current_user = twitter.current_user

    if not twitter.is_authenticated():
        return redirect('main:index')

    # closed_poll_tweets = Tweet.objects.filter(author=current_user, is_poll_open=False, poll__isnull=False).distinct().all()
    registering_tweets = Tweet.objects.filter(registered_user=current_user).all()

    return render(request, 'user.html', {
        # 'closed_poll_tweets': closed_poll_tweets,
        'registering_tweets': registering_tweets,
        'twitter': twitter,
    })

# show recent tweets
def user_recent(request):
    twitter = TwitterSessionOAuth(request)
    twitter_bearer = TwitterSessionBearer(request)
    current_user = twitter.current_user

    if not twitter.is_authenticated():
        return redirect('main:index')

    root: Dict = twitter_bearer.get_recent_user_tweets(current_user.remote_id, raw=True)

    tweets: List[Dict] = root.get('data', [])
    includes: Dict = root.get('includes', {})
    polls: List[Dict] = includes.get('polls', [])
    pollid2poll: Dict[str, Dict] = { poll['id']: poll for poll in polls }
    users: List[Dict] = includes.get('users', [])
    userid2user: Dict[str, Dict] = { user['id']: user for user in users }

    is_poll_open = False
    for poll in polls:
        poll['total_votes'] = sum([ option['votes'] for option in poll['options'] ])
        is_poll_open = is_poll_open or poll['voting_status'] == 'open'

        for option in poll['options']:
            option['rate'] = option['votes'] / poll['total_votes'] if poll['total_votes'] != 0 else 0.0
            option['percentage'] = option['rate'] * 100

    for tweet in tweets:
        _tweet = Tweet.objects.filter(remote_id=tweet['id']).first()
        can_register = (_tweet is None and is_poll_open) or (_tweet is not None and _tweet.can_register) # no data on server (but have poll) OR
        poll_data_on_service = _tweet is not None and _tweet.polls.count() > 0

        poll_ids = tweet.get('attachments', {}).get('poll_ids', [])
        tweet['author'] = userid2user[tweet['author_id']]
        tweet['polls'] = [ pollid2poll[poll_id] for poll_id in poll_ids ]
        tweet['can_register'] = can_register
        tweet['poll_data_on_service'] = poll_data_on_service


    return render(request, 'user_recent.html', {
        'tweets': tweets,
        'twitter': twitter,
    })

# user menu
def user_menu(request):
    twitter = TwitterSessionOAuth(request)

    if not twitter.is_authenticated():
        return redirect('main:index')

    return render(request, 'user_menu.html', {
        'twitter': twitter,
    })

# TODO: user can set public/private poll dynamics or random id?
# public view
def poll(request, tweet_id: int):
    twitter = TwitterSessionOAuth(request)
    current_user = twitter.current_user

    if not twitter.is_authenticated():
        return redirect('main:index')

    kwargs = {
        'remote_id': tweet_id,
        'poll__isnull': False,
        # 'author__protected': False,
    }

    if settings.CAN_REGISTER_SELF_TWEET_ONLY:
        kwargs['author'] = current_user

    tweet = Tweet.objects.filter(**kwargs).distinct().first()

    if tweet is None:
        return HttpResponse('Tweet Not Found or No Permission', status=403) # forbidden or badrequest, protected user

    return render(request, 'poll.html', {
        'tweet_id': tweet_id,
        'tweet': tweet,
        'twitter': twitter,
    })


@require_http_methods([ 'POST' ])
def register_poll(request, tweet_id: int = None):
    twitter = TwitterSessionOAuth(request)
    twitter_bearer = TwitterSessionBearer(request)
    current_user = twitter.current_user

    if not twitter.is_authenticated():
        return redirect('main:index')

    if tweet_id is None:
        tweet_id_or_url = request.POST['tweet_id_or_url']

        m = TWEET_ID_OR_URL_PATTERN.match(tweet_id_or_url)
        tweet_id = m.group('id') or m.group('id_in_url')
        if tweet_id is None:
            return HttpResponseBadRequest('Invalid tweet ID or URL')

    tweet_id = str(int(tweet_id))

    kwargs = {
        'remote_id': tweet_id,
    }

    if settings.CAN_REGISTER_SELF_TWEET_ONLY:
        kwargs['author'] = current_user

    tweet = Tweet.objects.filter(**kwargs).first()

    if tweet is not None and tweet.registered_user == current_user:
        return redirect('main:poll', tweet_id)

    if tweet is None or not tweet.has_poll_log:
        user_id_filter = None
        if settings.CAN_REGISTER_SELF_TWEET_ONLY:
            user_id_filter = [ current_user.remote_id ]

        try:
            with transaction.atomic():
                tweets = twitter_bearer.update_tweets(
                    tweet_ids=[ tweet_id ],
                    user_id_filter=user_id_filter,
                )
                if len(tweets) == 0:
                    raise BadRequestException('Tweet Not Found or No Permission') # forbidden or badrequest, protected user

                tweet = tweets[0]
                if not tweet.is_poll_open:
                    raise BadRequestException('Open Poll Not Found')

        except BadRequestException as err:
            return HttpResponseBadRequest(err.message)

    assert tweet is not None
    tweet.registered_user = current_user
    tweet.save()

    return redirect('main:poll', tweet_id)

@require_http_methods([ 'POST' ])
def unregister_poll(request, tweet_id: int):
    twitter = TwitterSessionOAuth(request)
    current_user = twitter.current_user

    if not twitter.is_authenticated():
        return redirect('main:index')

    tweet = Tweet.objects.filter(
        remote_id=tweet_id,
        author=current_user,
    ).distinct().first()

    if tweet is None:
        return HttpResponseBadRequest()

    tweet.registered_user = None
    tweet.save()

    return redirect('main:poll', tweet_id=tweet_id)

@require_http_methods([ 'POST' ])
def remove_poll(request, tweet_id: int):
    twitter = TwitterSessionOAuth(request)
    current_user = twitter.current_user

    if not twitter.is_authenticated():
        return redirect('main:index')

    tweet = Tweet.objects.filter(
        remote_id=tweet_id,
        author=current_user,
    ).distinct().first()

    if tweet is None:
        return HttpResponseBadRequest()

    tweet.delete()
    return redirect('main:user')

@require_http_methods([ 'POST' ])
def user_unregister_polls(request):
    twitter = TwitterSessionOAuth(request)
    if not twitter.is_authenticated():
        return HttpResponse('Forbidden', code=403)

    current_user = twitter.current_user

    for tweet in Tweet.objects.filter(registered_user=current_user):
        tweet.registered_user = None
        tweet.save()

    return redirect('main:user_menu')

@require_http_methods([ 'POST' ])
def user_remove_polls(request):
    twitter = TwitterSessionOAuth(request)
    if not twitter.is_authenticated():
        return HttpResponse('Forbidden', code=403)

    current_user = twitter.current_user

    Poll.objects.filter(tweet__author=current_user).delete()
    # Poll.objects.filter(tweet__registered_user=current_user).delete()

    for tweet in Tweet.objects.filter(registered_user=current_user):
        tweet.registered_user = None
        tweet.save()

    return redirect('main:user_menu')

# temporary
# def update(request, tweet_id: int):
#     twitter = TwitterSessionOAuth(request)
#     twitter_bearer = TwitterSessionBearer(request)
#
#     tweets: List[Tweet] = twitter_bearer.update_tweets(tweet_ids=[ str(tweet_id), ])
#
#     return redirect('main:poll', tweet_id=tweet_id)


# start oauth (redirect to twitter.com)
@require_http_methods([ 'POST' ])
def oauth(request):
    twitter = TwitterSessionOAuth(request)
    try:
        response = twitter.start_oauth()
    except requests_oauthlib.oauth1_session.TokenRequestDenied as err:
        print(err)
        return HttpResponse('Token request to Twitter failed with code %d.' % err.status_code, status=400)

    return response

# redirected from twitter.com
def oauth_callback(request):
    twitter = TwitterSessionOAuth(request)

    try:
        twitter.on_oauth_callback()
    except requests_oauthlib.oauth1_session.TokenRequestDenied as err:
        # Token request failed with code 401, response was '現在この機能は一時的にご利用いただけません'
        print(err)
        return HttpResponse('Token request to Twitter failed with code %d.' % err.status_code, status=400)

    return redirect('main:user')

# remove tokens from DB
@require_http_methods([ 'POST' ])
def oauth_remove(request):
    twitter = TwitterSessionOAuth(request)

    response = redirect('main:index')
    twitter.remove_oauth(response)

    return response

# remove tokens from user session
@require_http_methods([ 'POST' ])
def oauth_logout(request):
    twitter = TwitterSessionOAuth(request)

    response = redirect('main:index')
    twitter.logout(response)

    return response
