from flask import Flask, request, abort, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import os, requests, time, base64, traceback, pickle, redis, secrets
from config import DevelopmentConfiguration
import dateutil.parser as dp

# create a connection to the redis instance
redis_instance = redis.from_url("redis://127.0.0.1:6379")


load_dotenv()
app = Flask(__name__, static_folder='../build')
app.config.from_object(DevelopmentConfiguration)

CORS(app, supports_credentials=True)

# Login API route:

@app.route('/callback')
def auth_callback():

    # wrap the code in try and except block so in case anything happens - e.g., authorisation code is invalid, etc. we can handle it.
    # later perhaps redirect the user back to the connect page to ask them to try connecting through strava again.
    flaskResponse = {"error": False}
    
    # first check see whether the user trying to access this endpoint is already authenticated or not - if not, go ahead and try exchange the provided authorisation code
    # for an access and a refresh token. 

    # check see if a session-id is included in the request header and see if there is a matching session in redis already:
    user_session = get_session(request.headers.get("session-id"))

    if not user_session.get('user_id'):
        try:
            # create a session object which we will save on redis - this object will hold relevant information for the duration that the user is logged in.
            session = {}
            # generate a session_id - we will save the session object against this id in redis.
            session_id = secrets.token_hex(16)

            auth_code = request.args.get('code')
            
            auth_payload = {'client_id': os.environ.get('STRAVA_CLIENT_ID'),
                            'client_secret': os.environ.get('STRAVA_CLIENT_SECRET'),
                                'code': auth_code,
                                    'grant_type': 'authorization_code'}


            # exchange the authorisation code for access and refresh token:
            response = requests.post('https://www.strava.com/oauth/token', params=auth_payload)

            response_data = response.json()

            # store the access and refresh tokens in the server-side session:

            session["athlete_info"] = response_data["athlete"]
            session['user_id'] = response_data["athlete"]["id"]
            session['access_token'] = response_data["access_token"]
            session['refresh_token'] = response_data["refresh_token"]
            session['access_token_expirytime'] = response_data["expires_at"]

            # since cross-site cookies are not allowed we send the session id in the response body:
            flaskResponse["session_id"] = session_id
            flaskResponse["user_data"] = response_data["athlete"]

            # serialise the session object using the pickle module
            # this is the module used by flask-session - serialisation helps with transportation and storage of our data
            serialised_session = pickle.dumps(session)

            # save this session in redis:
            redis_instance.set(session_id, serialised_session)

        except Exception:
            flaskResponse["error"] = True
            print(str(Exception))
            print("printing the response after error invoked inside except", flaskResponse)

            return flaskResponse
    else:
        flaskResponse["user_data"] = user_session.get("athlete_info")

    return flaskResponse


@app.route('/authentication_status')
def check_authentication():
    # try get the user_id of the session corresponding to the current user
    # if the current user has a session id corresponding to a valid session stored within their cookies, this will return a user_id
    # otherwise it will return None in which case the user is not authenticated and needs to login.

    user_session = get_session(request.headers.get("session-id"))
    user_id = user_session.get('user_id')
    
    if not user_id:
        return jsonify(result=False)

    # check see whether the user has their lastFM account connected:
    last_fm_status = bool(user_session.get('last_fm_username'))

    return jsonify(result=True, last_fm_status=last_fm_status)



@app.route('/get_user_activity_data')
def get_user_activity_data():
    
    session_id = request.headers.get('session-id')
    user_session = get_session(session_id)

    # check see if a valid user-session exists - this endpoint is only for authorised users
    if user_session.get('user_id'): 

        # get the start and end date:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        # check see if we already have the user's running activities stored in the session or not - if we do return that immediately
        # otherwise retrieve it using the STRAVA Api:
        # if the user has specified start and end date by pass this and retrieve the data from Strava.

        if (user_session.get('run_activities') and not (start_date or end_date)):
            return jsonify(error_status=False, athlete_running_data=user_session.get('run_activities'))
       
        else:
            # check see if the access token has expired, if it has refresh it:
            try:
                check_access_token_status()
                user_session = get_session(session_id)
            except:
                # if for any reason we get an error return True for error status
                return jsonify(error_status=True)
            

            all_strava_activities = []
            page_number = 1

            request_payload = {
                'per_page':200, 'page': page_number
            }

            if start_date: request_payload['after'] = start_date
            if end_date: request_payload['before'] = end_date

            # get all of user's activities: 
            while True:
                activity_response = requests.get('https://www.strava.com/api/v3/athlete/activities',
                                                params=request_payload,
                                                headers={"Authorization": f"Bearer {user_session.get('access_token')}"})
                retrieved_data = activity_response.json()
                all_strava_activities.extend(retrieved_data)
                # increment page number
                page_number += 1

                if len(retrieved_data) != 200:
                    # if we have retrieved less than 200 activities that means we have reached the end of user's activities and so we should break out of the loop
                    break
            
            # filter the retrieved activities to just the running activities
            run_activities = [activity for activity in all_strava_activities if activity['type'] == 'Run']

            # if we have retrieved all user activities (i.e., no date filter was specified by the user) save the activities in current session:
            if (not (start_date or end_date)):

                user_session['run_activities'] = run_activities
                # reserialise this user_session object using pickle module and set the updated object against the session_id in redis:
                updated_user_session = pickle.dumps(user_session)
                redis_instance.set(session_id, updated_user_session)

            return jsonify(error_status=False, athlete_running_data=run_activities)
    else: 
        abort(401)


@app.route('/get_user_profile_data')
def get_user_profile_data():
    # pass in the session-id included in the request header to get_session function which checks to see
    # if a valid session with the given id exists, and if it does it returns it back to the caller.
    session_id = request.headers.get("session-id")
    user_session = get_session(session_id)

    # check see if a valid user-session exists - this endpoint is only for authorised users
    if user_session.get('user_id'):
        # check see if the access token has expired, if it has refresh it:
        try:
            check_access_token_status()
            user_session = get_session(session_id)
        except:
            # if for any reason we get an error return True for error status
            return jsonify(error_status=True)

        # get user profile data from Strava:
        user_response = requests.get('https://www.strava.com/api/v3/athlete',
                                     headers={"Authorization": f"Bearer {user_session.get('access_token')}"})
        user_response_data = user_response.json()
        

        return jsonify(error_status=False, athlete_data=user_response_data)

    else:
        abort(401)


@app.route('/get_activity_strava_data')
def get_activity_strava_data():
    # check see if a valid user-session exists - this endpoint is only for authorised users
    session_id = request.headers.get("session-id")
    user_session = get_session(session_id)

    if user_session.get('user_id'):
        # check see if the access token has expired, if it has refresh it:
        try:
            check_access_token_status()
            user_session = get_session(session_id)
        except:
            # if for any reason we get an error return True for error status
            return jsonify(error_status=True)

        try: 
            # get activity id from the query URL:
            activity_id = request.args.get('activity_id')

            # try retrieve the activity data from session - if it doesn't exist retrieve it from Strava API:
            if (user_session.get(f'{activity_id}_data')):
                activity_response_data = user_session.get(f'{activity_id}_data')

            else:
                # get the activity data from Strava:
                activity_response = requests.get(f'https://www.strava.com/api/v3/activities/{activity_id}',
                                            headers={"Authorization": f"Bearer {user_session.get('access_token')}"})
                activity_response_data = activity_response.json()

                # save the activity data in the user session so if the same user visits the same activity multiple times we won't have to retrieve it
                # from strava and can retrieve it from session instead:
                user_session[f'{activity_id}_data'] = activity_response_data

            # save the activity stream data in the user session so if the same user visits the same activity multiple times we won't have to retrieve it
            # from strava and can retrieve it from session instead:

            if (user_session.get(f'{activity_id}_streams')):
                activity_stream_response_data = user_session.get(f'{activity_id}_streams')
            else:  
                # get activity velocity and latitude/longitude streams from Strava:
                activity_stream_response = requests.get(f'https://www.strava.com/api/v3/activities/{activity_id}/streams',
                                            params={'series_type': 'time', 'keys':'latlng,velocity_smooth', 'key_by_type':'true'},
                                            headers={"Authorization": f"Bearer {user_session.get('access_token')}"})
                activity_stream_response_data = activity_stream_response.json()
                user_session[f'{activity_id}_streams'] = activity_stream_response_data


            # update the user session stored on redis:
            redis_instance.set(session_id, pickle.dumps(user_session))

            if activity_response_data.get('errors'):
                # if we retrieved an error from Strava, abort this request with an error
                raise Exception("Failed to retrieve activity")
                        
        except Exception as e:
            traceback.print_exc()
            print(e)
            return jsonify(error_status=True)
        return jsonify(error_status=False, activity_data=activity_response_data, activity_streams=activity_stream_response_data)
    else:
        abort(401)


# following function will retrieve the music tracks listened to by the user during their running activity:
@app.route('/get_activity_music_data')
def get_activity_music_data():
    # check see if a valid user-session exists - this endpoint is only for authorised users
    session_id = request.headers.get("session-id")
    user_session = get_session(session_id)

    if user_session.get('user_id'):
        activity_id = request.args.get('activity_id')

        # check see if we have the data associated with this activity saved in the session - if so retrieve it - otherwise use Strava API
        if (user_session.get(f'{activity_id}_data')):
            activity_response_data = user_session.get(f'{activity_id}_data')
        else:
            # check see if the access token has expired, if it has refresh it:
            try:
                check_access_token_status()
                user_session = get_session(session_id)
            except:
                # if for any reason we get an error return True for error status
                return jsonify(error_status=True)
            
            activity_response_data = requests.get(f'https://www.strava.com/api/v3/activities/{activity_id}',
                                            headers={"Authorization": f"Bearer {user_session.get('access_token')}"}).json()
        
        # get activity start and end times
        # convert the start-time from ISO 8601 format to Unix timestamp format:
        parsed_start_time = dp.parse(activity_response_data.get('start_date'))
        start_time_unix = int(parsed_start_time.timestamp())
        end_time_unix = start_time_unix + activity_response_data.get('elapsed_time')

        # check see whether the user session already holds the music data associated to this running activity - if not retrieve the data:
        if (user_session.get(f'{activity_id}_music_data')):
            music_list = user_session.get(f'{activity_id}_music_data')
        else:
            # use the activity start time to retrieve the appropriate music data from Last.Fm
            try:        
                # allow for 5 minute buffer zone - this way we can capture any song that started playing before the start of the running activity.
                buffered_start_time_unix = start_time_unix - (5 * 60)

                request_payload = {'method': 'user.getrecenttracks',
                    'user': user_session.get('last_fm_username'),
                    'api_key': os.environ.get('LAST_FM_API_KEY'),
                    'from': buffered_start_time_unix,
                    'to': end_time_unix,
                    'format': 'json'}

                music_response = requests.post('http://ws.audioscrobbler.com/2.0/', params=request_payload)
                music_response_data = music_response.json()
                music_list = music_response_data.get('recenttracks').get('track')
                
                # if the user is currently listening to a music this music will also be retrieved - we don't want this included in the list of retrieved tracks
                # the music currently playing will have an "@attr" field so we will filter it out:
                
                # if only a single music track is retrieved, Last.FM returns an object and not a list which would cause bugs - so we convert it to a list.
                if not isinstance(music_list, list):
                    music_list = [music_list]

                music_list = [music for music in music_list if not music.get('@attr')]


                # retrieve info about the music (this is so that we can get the duration of the tracks):
                for index, track in enumerate(music_list):
                    track_request_payload = {'method': 'track.getInfo',
                                        'api_key': os.environ.get('LAST_FM_API_KEY'),
                                        'track': track.get('name'),
                                        'artist': track.get('artist').get('#text'),
                                        'format': 'json'}
                    
                    track_info_response = requests.post('http://ws.audioscrobbler.com/2.0/', params=track_request_payload)
                    track_info_data = track_info_response.json()

                    # include the retrieved duration in the music_list array - note some music (if unpopular) may not have much associated metadata
                    # in which case the retrieved duration would be 0. 
                    music_list[index]['duration'] = track_info_data.get('track').get('duration')
                    music_list[index]['music_extra_info'] = track_info_data.get('track')

                # filter out any of the songs that ended before the start of the activity - this could happen due to our 5 minute buffer period before start of activity:
                
                music_list = [music for music in music_list if not ((int(music['duration']) / 1000) + int(music['date']['uts'])) < start_time_unix]
                
                # save the music_list against the activity id in the user session:
                user_session[f'{activity_id}_music_data'] = music_list

                # update the user session on redis:
                redis_instance.set(session_id, pickle.dumps(user_session))

            except Exception as e:
                traceback.print_exc()
                print(e)
                return jsonify(error_status=True)
        
        return jsonify(music_data=music_list, activity_times={'startTime':start_time_unix, 'endTime':end_time_unix})
    
    else:
        abort(401)


# the following function retrieves information about the artists whose music the user listened to during the activity:
@app.route('/get_music_artist_data')
def get_music_artist_data():
    # check see if a valid user-session exists - this endpoint is only for authorised users
    session_id = request.headers.get("session-id")
    user_session = get_session(session_id)

    if user_session.get('user_id'):
        # initialise lists which are going to hold the information retrieved from spotify
        spotify_data_list = []
            
        try:     
            # retrieve the activity id from the url query string:
            activity_id = request.args.get('activity_id')

            if (user_session.get(f'{activity_id}_spotify_data')):
                spotify_data_list = user_session.get(f'{activity_id}_spotify_data')

            else:
                # retrieve the list of music corresponding to this activity from the session:
                music_list = user_session.get(f'{activity_id}_music_data')

                # for each music in the list retrieve info regarding the artist.
                # for each music in the list retrieve similar tracks and also for each artist retrieve their top tracks
                for track in music_list:
                    # retrieve the spotify id for each music track:
                    track_params = {
                        'q': f'{track.get("album").get("#text")} {track.get("artist").get("#text")} {track.get("name")}',
                        'type': ['track'],
                        'limit': 1
                    }
                    artist_params = {
                        'q': track.get("artist").get("#text"),
                        'type': ['artist'],
                        'limit': 1
                    }
                    music_id = retrieve_data_from_spotify(session_id, user_session, 'search', track_params).get('tracks').get('items')[0].get('id')
                    artist_data = retrieve_data_from_spotify(session_id, user_session, 'search', artist_params).get('artists').get('items')[0]
                    artist_id = artist_data.get('id')

                    # use the music id to retrieve further information about this track:
                    music_data = retrieve_data_from_spotify(session_id, user_session, f'tracks/{music_id}')

                    # use aritst id to retrieve their top tracks:
                    top_tracks_data = retrieve_data_from_spotify(session_id, user_session, f'artists/{artist_id}/top-tracks', {'market': 'GB'}).get('tracks')

                    # create a list of the relevant data for these top tracks (remove the current track if it's within this list)
                    top_tracks_list = []
                    for top_track in top_tracks_data:
                        if top_track.get('name') != music_data.get('name'):
                            track_relevant_data = {}

                            try:
                                track_relevant_data['image'] = top_track.get('album').get('images')[0].get('url') 
                            except Exception:
                                track_relevant_data['image'] = None
                                
                            track_relevant_data['preview_url'] = top_track.get('preview_url')
                            track_relevant_data['track_name'] = top_track.get('name')
                            track_relevant_data['artist'] = artist_data.get('name')
                            top_tracks_list.append(track_relevant_data)
                    
                    # use the current artist and track to get a list of recommended tracks similar to this:
                    recommended_music_params = {
                        'seed_artist': artist_id,
                        'seed_tracks': music_id,
                        'limit': 15
                    }
                    recommended_tracks_data = retrieve_data_from_spotify(session_id, user_session, f'recommendations', params=recommended_music_params).get('tracks')
                    
                    # filter out any songs by the same artist:
                    recommended_tracks_list = []
                    for recommended_track in recommended_tracks_data:
                        if recommended_track.get('album').get('artists')[0].get('name') != artist_data.get('name'):
                            track_relevant_data = {}
                            try:
                                track_relevant_data['image'] = recommended_track.get('album').get('images')[0].get('url')
                            except Exception:
                                track_relevant_data['image'] = None
                                
                            track_relevant_data['preview_url'] = recommended_track.get('preview_url')
                            track_relevant_data['track_name'] = recommended_track.get('name')
                            track_relevant_data['artist'] = recommended_track.get('album').get('artists')[0].get('name')
                            recommended_tracks_list.append(track_relevant_data)


                    # if the length of the list is greater than 10, limit it to 10:
                    if len(recommended_tracks_list) > 10:
                        recommended_tracks_list = recommended_tracks_list[0:10]


                    # create an object containing all the retrieved data:
                    spotify_retrieved_data = {
                        'current_track': {
                            'name': music_data.get('name'),
                            'artist': {
                                'name': artist_data.get('name'),
                                'image': artist_data.get('images')[0].get('url'),
                                'top_tracks': top_tracks_list
                            },
                            'preview': music_data.get('preview_url'),
                            'genres': artist_data.get('genres'),
                        },
                        'recommended_tracks': recommended_tracks_list
                    }

                    spotify_data_list.append(spotify_retrieved_data)

                    # add this to the session and save the new session on redis:
                    user_session[f'{activity_id}_spotify_data'] = spotify_data_list
                    redis_instance.set(session_id, pickle.dumps(user_session))

        except Exception as e:
            traceback.print_exc()
            print(e)
            return jsonify(error_status=True)
            
        return jsonify(spotify_data_list)
    
    else:
        abort(401)



# following function will refresh the user's access token if it has expired:
def check_access_token_status():
    session_id = request.headers.get("session-id")
    user_session = get_session(session_id)

    # check for a valid user session:
    if user_session.get('user_id'):
        # check to see if access token is expired:
        expiry_time = user_session.get('access_token_expirytime')
        # if access token has expired refresh it - else do nothing
        if time.time() > expiry_time:
            try:
                # request for a new access token:
                request_payload = {'client_id': os.environ.get('STRAVA_CLIENT_ID'),
                                'client_secret': os.environ.get('STRAVA_CLIENT_SECRET'),
                                'grant_type': 'refresh_token',
                                'refresh_token': user_session.get('refresh_token')}

                # exchange the authorisation code for access and refresh token:

                response = requests.post('https://www.strava.com/oauth/token', params=request_payload)
                response_data = response.json()

                print("Access token refreshed: ", response_data['access_token'])

                # update the values stored in the user session:
                user_session['access_token'] = response_data["access_token"]
                user_session['refresh_token'] = response_data["refresh_token"]
                user_session['access_token_expirytime'] = response_data["expires_at"]

                # update the session object stored on redis:
                redis_instance.set(session_id, pickle.dumps(user_session))

            except:
                raise Exception("Access token refresh failed.")

    else:
        raise Exception("Valid user session not found.")
    



def retrieve_data_from_spotify(session_id, user_session, url_endpoint, params=""):
    access_token = refresh_spotify_access_token(session_id, user_session)
    authorisation_header = {"Authorization": "Bearer " + access_token}
    base_url = 'https://api.spotify.com/v1/'
    response = requests.get(f'{base_url}{url_endpoint}', params=params, headers=authorisation_header)
    response_data = response.json()
    return response_data


def refresh_spotify_access_token(session_id, user_session):
    # check see if we already have an access token saved in the user session and whether the access token is still valid.
    # if it is return it, otherwise request a new access token and return the new one.

    if user_session.get('spotify_access_token') and (time.time() < user_session.get('spotify_access_token').get('expiry_time')):
        return(user_session.get('spotify_access_token').get('token'))
    
    else:
        data = {'grant_type': 'client_credentials'}
        authorisation_string = os.environ.get('SPOTIFY_CLIENT_ID') + ':' + os.environ.get('SPOTIFY_CLIENT_SECRET')
        authorisation_bytes = authorisation_string.encode('utf-8')
        authorisation_base64 = str(base64.b64encode(authorisation_bytes), "utf-8") 
        spotify_auth_response = requests.post('https://accounts.spotify.com/api/token', 
                                            data=data,
                                            headers={"Authorization" : "Basic " + authorisation_base64,
                                                        "Content-Type": "application/x-www-form-urlencoded"})
        
        # we want to refresh the access token if more than half of its expiry preiod has passed (hence the division by 2):
        expiry_time = time.time() + (spotify_auth_response.json().get('expires_in') / 2)
        user_session['spotify_access_token'] = {'token': spotify_auth_response.json().get('access_token'), 'expiry_time': expiry_time}
        
        # update user session in redis:
        redis_instance.set(session_id, pickle.dumps(user_session))

        return(spotify_auth_response.json().get('access_token'))


# following function checks to see if a session with the provided session id exists and if it does it deserialises and returns it:
def get_session(session_id):
    if session_id:
        user_session = redis_instance.get(session_id)
    else:
        return {}

    if user_session:
        # flask-session uses the pickle module to serialise session data. So, we need to use it to deserialise it. 
        user_session = pickle.loads(user_session)
        
        return user_session

    else: return {}


@app.route('/logout')
def logout_user():
    # delete the server-side session
    session_id = request.headers.get("session_id")
    redis_instance.delete(session_id)

    return jsonify({'logout-status': True}), 200


@app.route('/retrieve_lastfm')
def retrieve_lastfm_user():
    session_id = request.headers.get("session-id")
    user_session = get_session(session_id)
    responseObject = {"error": False}

    try:
        username = request.args.get('username')

        request_payload = {'method': 'user.getinfo',
                        'user': username,
                        'api_key': os.environ.get('LAST_FM_API_KEY'),
                        'format': 'json'}

        # check see if an account exists with the provided details:

        response = requests.post('http://ws.audioscrobbler.com/2.0/', params=request_payload)
        response_data = response.json()

        # check see if the retrieved response has any error
        if response_data.get("error"):
            raise Exception("Access token refresh failed.")

        # set the user's lastFM account in the session:
        user_session['last_fm_username'] = username

        # update the redis user session:
        redis_instance.set(session_id, pickle.dumps(user_session))
        
    except:
        responseObject = {"error": True}
    
    return responseObject


if __name__ == "__main__":
    app.run(debug=True)