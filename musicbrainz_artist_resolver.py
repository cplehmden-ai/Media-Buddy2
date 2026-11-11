"""MusicBrainz artist MBID resolver shared by the music-video scraper.

Uses the Test 63 artist/recording matching and the existing JSON cache.
This module does not change NFOs or download artwork.
"""
from __future__ import annotations
import time
import requests
from pathlib import Path
from musicbrainz_diagnostic import (DEFAULT_UA, DEFAULT_CACHE, clean, load_cache,
    save_cache, request_with_retries, inspect_response)

class ArtistMBIDResolver:
    def __init__(self, timeout, user_agent=None, interval=2.0, attempts=4, max_wait=30.0):
        self.timeout=timeout
        self.interval=max(1.1, float(interval))
        self.attempts=max(1, int(attempts))
        self.max_wait=max(1.0, float(max_wait))
        self.cache=load_cache(DEFAULT_CACHE)
        self.session=requests.Session()
        self.session.headers.update({'User-Agent':user_agent or DEFAULT_UA,'Accept':'application/json'})
        original_get=self.session.get
        last_request=[0.0]
        def throttled_get(*args,**kwargs):
            remaining=self.interval-(time.monotonic()-last_request[0])
            if last_request[0] and remaining>0: time.sleep(remaining)
            last_request[0]=time.monotonic()
            return original_get(*args,**kwargs)
        self.session.get=throttled_get

    def resolve(self, artist, song):
        key=clean(artist)
        cached=self.cache.get(key)
        if cached and cached.get('mbid') and cached.get('artist')==artist:
            # Verified artist identity can be reused for any track by that artist.
            # Older cache entries may be incorrectly labelled unique (Test 62),
            # so they are rechecked once when a song is available.
            if cached.get('match_status')=='track_verified' or cached.get('ambiguity_checked') or not song:
                print(f'[MBID CACHE HIT] {artist} | MBID={cached["mbid"]} | status={cached.get("match_status")}')
                return cached
        response,error=request_with_retries(self.session,artist,self.timeout,self.attempts,self.interval,self.max_wait)
        if response is None or response.status_code != 200:
            print(f'[MBID WARN] {artist} | lookup unavailable: {error or (response.status_code if response is not None else "no response")}; continuing')
            # Do not silently use an unverified, potentially wrong MBID.
            return cached if cached and cached.get('match_status')=='track_verified' else None
        result=inspect_response(response,artist,song=song,session=self.session,
            timeout=self.timeout,attempts=self.attempts,interval=self.interval,max_wait=self.max_wait)
        if not result:
            print(f'[MBID WARN] {artist} | no reliable match; continuing')
            return None
        result['ambiguity_checked']=True
        self.cache[key]=result
        try: save_cache(DEFAULT_CACHE,self.cache)
        except OSError as exc: print(f'[MBID CACHE WARN] {exc}')
        print(f'[MBID RESULT] {artist} | MBID={result["mbid"]} | status={result["match_status"]} | song={song}')
        return result

    def close(self):
        self.session.close()
