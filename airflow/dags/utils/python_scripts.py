import json
import logging
from tempfile import NamedTemporaryFile

import duckdb
import pandas as pd
import requests
from airflow.models import Variable
from airflow.providers.microsoft.azure.hooks.wasb import WasbHook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from dotenv import load_dotenv
from utils import udfs

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(format='%(asctime)s %(levelname)s:%(name)s:%(message)s')
logging.getLogger().setLevel(20)


# Define Variables
storage_account_name = "rbchesssa"
container_name = "chess-etl-files"
az_hook = WasbHook(wasb_conn_id="azure_chess_storage_uri")  
psql_hook = PostgresHook(postgres_conn_id="azure_chess_dw")
file_path_template = "az://rbchesssa.blob.core.windows.net/chess-etl-files/{file_name}"


def extract_and_load_chess_data(username: str, year: int, month: int) -> list:
    """
    Fetch chess game data for a specific user and month from Chess.com API.
    :param username: chess.com username
    :param year: Year of the games
    :param month: Month of the games (1-12)
    :return: List of games in JSON format

    """

    # The headers are used to mimic a browser request because it returns a 403 error if it detects that it's a bot
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"
    }

    logging.info(f"Attempting to fetch data for Year: {year} and Month: {month}, formatted as {int(month):02}")

    url = f"https://api.chess.com/pub/player/{username}/games/{year}/{int(month):02}"
    logging.info(f"Attemping to Fetch Data from: {url}")
    response = requests.get(url, headers=headers)

    pulled_data = []

    if response.status_code == 200:
        data = response.json()
        pulled_data = data.get("games", [])
        logging.info(
            f"Successfully Fetched Data From CHess.com API, Records Pulled: {len(pulled_data)}"
        )
    else:
        logging.info(f"Failed to fetch data: {response.status_code}")
        pulled_data = []

    # Upload the data to Azure Storage
    with NamedTemporaryFile("w") as temp:
        logging.info("Attempting to Upload Data to Azure Storage")
        json.dump(pulled_data, temp)
        temp.flush()
        az_hook = WasbHook(wasb_conn_id="azure_chess_storage")

        blobname = f"bronze/{year}-{int(month):02}-games.json"
        az_hook.load_file(
            file_path=temp.name,
            container_name=container_name,
            blob_name=blobname,
            overwrite=True,
        )

        logging.info(f"Successfully Uploaded Data to Azure Storage as {blobname}")
        return True


def preview_dataframe(data_frame) -> str:

    logging.info("Previewing Dataframe")
    logging.info(data_frame.dtypes)
    con = duckdb.connect(":memory:")
    # Preview the first n rows of a dataframe
    table = con.execute(f"SELECT * FROM data_frame LIMIT 5").fetchall()
    formatted_output = "\n".join(
        ["\t".join(map(str, row)) for row in table]
    )  # Convert to string
    logging.info(formatted_output)

def initialize_azure_extension():
    """Initialize a DuckDB connection with Azure extension.
    This function sets up a DuckDB in-memory database connection, installs and loads 
    the Azure extension, configures a secret for the Azure connection string, and 
    applies necessary transport options. Additionally, it initializes user-defined 
    functions (UDFs) for further use.

    Returns:
        duckdb.DuckDBPyConnection: Configured DuckDB connection with Azure extension.
    """
    conn_string = Variable.get("AZURE_STORAGE_CONN_STRING_SECRET")

    conn = duckdb.connect(":memory:")
    conn.sql(
        F"""INSTALL azure; 
        LOAD azure;
        
        -- Create a secret for the connection string
        CREATE SECRET azure_adls_secret (
        TYPE azure,
        CONNECTION_STRING '{conn_string}' );

        -- Set the azure_transport_option_type to curl to avoid read error
        SET azure_transport_option_type = 'curl';
        """
    )

    # add all the required user defined functions
    conn = udfs.initialize_udfs(conn)

    return conn


def upload_duckdb_to_azure(
    duckdb_result: duckdb.DuckDBPyRelation, 
    container_name: str, 
    blob_name: str
) -> None:
    """
    Uploads a DuckDB result to Azure Blob Storage as a Parquet file.
    Args:
        duckdb_result (duckdb.DuckDBPyRelation): The DuckDB result to upload.
        container_name (str): The name of the Azure Blob Storage container.
        blob_name (str): The name of the blob in Azure Blob Storage.
    """ 
    logging.info(f"File To be loaded has {duckdb_result.shape}")
    with NamedTemporaryFile("w", suffix=".parquet") as temp_file:
        df = duckdb_result.fetchdf()

        df.to_parquet(temp_file.name, index=False)
        az_hook.load_file(
            file_path=temp_file.name,
            container_name=container_name,
            blob_name=blob_name,
            overwrite=True,
        )
    logging.info(f"Successfully Loaded to Azure Storage as {blob_name}")


def transform_json_to_fact_table(year: int, month: int, **kwargs) -> None:
    """
    Transforms JSON file in bronze layer fact table format and uploads it to Silver layer.
    Args:
        year (int): The year of the games to process.
        month (int): The month of the games to process.
        **kwargs: Additional keyword arguments, including Airflow task instance for XCom operations.
    Returns:
        None
    """

    con = initialize_azure_extension()
    source_blob_name = f"bronze/{year}-{int(month):02}-games.json"
    destination_blob_name = f"silver/fact-{year}-{int(month):02}-games.parquet"

    logging.info(f"Attempting to Transform Data from {source_blob_name}")
    fct = con.sql(
        """SELECT url as game_url,
            time_control as time_control,
            rated as rated,
            time_class as time_class,
            rules as rules,
            white.rating as white_rating,
            white.result as white_result,
            black.rating as black_rating,
            black.result as black_result,    
            REGEXP_EXTRACT(pgn, '\[Event "(.*?)"', 1) as pgn_event,
            REGEXP_EXTRACT(pgn, '\[Site "(.*?)"', 1) as pgn_site,
            STRPTIME(REPLACE(REGEXP_EXTRACT(pgn, '\[Date "(.*?)"', 1), '.', '/'), '%Y/%m/%d')::DATE AS game_date, 
            REGEXP_EXTRACT(pgn, '\[White "(.*?)"', 1) as pgn_white_user,   
            REGEXP_EXTRACT(pgn, '\[Black "(.*?)"', 1) as pgn_black_user,
            REGEXP_EXTRACT(pgn, '\[Result "(.*?)"', 1) as pgn_result,
            REGEXP_EXTRACT(pgn, '\[CurrentPosition "(.*?)"', 1) as pgn_current_position,
            REGEXP_EXTRACT(pgn, '\[Timezone "(.*?)"', 1) as pgn_timezone,
            REGEXP_EXTRACT(pgn, '\[ECO "(.*?)"', 1) as pgn_eco,
            REGEXP_EXTRACT(pgn, '\[ECOUrl "(.*?)"', 1) as pgn_eco_url,
            STRPTIME(REGEXP_EXTRACT(pgn, '\[StartTime "(.*?)"', 1), '%H:%M:%S'):: TIME as start_time,
            STRPTIME(REGEXP_EXTRACT(pgn, '\[EndTime "(.*?)"', 1), '%H:%M:%S'):: TIME as end_time,
                            STRPTIME(REPLACE(REGEXP_EXTRACT(pgn, '\[EndDate "(.*?)"', 1), '.', '/'), '%Y/%m/%d')::DATE AS end_game_date,
            ARRAY_TO_STRING(REGEXP_EXTRACT_ALL(pgn, '\. (.*?) {\[', 1), ' ') as pgn_raw,
            add_move_numbers(REGEXP_EXTRACT_ALL(pgn, '\. (.*?) {\[', 1)) as pgn_trans"""
        + f" FROM '{file_path_template.format(file_name=source_blob_name)}'"
    ).fetchdf()

    # Ensure Data accuracy by ensuring that the dates are in the correct format.
    fct["start_time"] = pd.to_datetime(
        fct["game_date"].astype(str) + " " + fct["start_time"].astype(str),
        format="%Y-%m-%d %H:%M:%S",
    )
    fct["end_time"] = pd.to_datetime(
        fct["end_game_date"].astype(str) + " " + fct["end_time"].astype(str),
        format="%Y-%m-%d %H:%M:%S",
    )

    # Upload the file to silver layer
    fct = con.from_df(fct)
    upload_duckdb_to_azure(fct, container_name=container_name, blob_name=destination_blob_name)

    # Push File name to xcom
    ti = kwargs["ti"]
    ti.xcom_push(key="fact_blob_name", value=destination_blob_name)

    logging.info(f"Successfully Transformed Data and Loaded to Azure Storage as {destination_blob_name}")


########################################### Loading Sripts for Gold Layer ##############################


def load_dim_openings(**kwargs):
    """
        Loads and updates the dimensional table for chess openings in Gold Layer.
        Extracts only chess opening details and appends them to an existing dimensional 
        table or creates a new one if it doesn't exist.

    Args:
        **kwargs: Arbitrary keyword arguments passed from the Airflow task, 
                  including the task instance (`ti`) for XComs interaction.

    Returns:
        None
    """

    # Extract filename from airflow xcoms from previous run
    ti = kwargs["ti"]
    filename = ti.xcom_pull(
        task_ids="transform_json_to_fact_table",
        dag_id="pull_data_from_chess_api",
        key="fact_blob_name",
    )
    # filename  = kwargs['dim_destination']
    logging.info(f"Received File Name: {filename}")

    con = initialize_azure_extension()

    dim_file_name = "gold/dim_openings.parquet"
    source_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{filename}"
    destination_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{dim_file_name}"

    # Check existence of file in Azure storage
    file_exists = az_hook.check_for_blob(
        container_name=container_name, blob_name=dim_file_name
    )

    if file_exists:
        cur_dim_openings = con.sql(
            f"""SELECT DISTINCT pgn_eco_url, 
                        extract_opening_name(pgn_eco_url) as opening_name,
                        get_opening_family(opening_name) as opening_family,
                        get_opening_variation(opening_name) as opening_variation,
                        pgn_eco as eco_code 
                
                FROM '{source_file_path}'
                WHERE pgn_eco_url NOT IN (
                    SELECT pgn_eco_url 
                    FROM '{destination_file_path}'
                        )
                UNION  -- Simply append existing data

                SELECT * FROM '{destination_file_path}';                                 
                                """
        ).fetchdf()
    else:
        cur_dim_openings = con.sql(
            f"""SELECT DISTINCT pgn_eco_url, 
                        extract_opening_name(pgn_eco_url) as opening_name,
                        get_opening_family(opening_name) as opening_family,
                        get_opening_variation(opening_name) as opening_variation,
                        pgn_eco as eco_code
                    FROM '{source_file_path}'; """
        ).fetchdf()

    # Create NamedTemp File and upload to azure
    with NamedTemporaryFile("w", suffix=".parquet") as temp_file:
        cur_dim_openings.to_parquet(temp_file.name, index=False)
        az_hook.load_file(
            file_path=temp_file.name,
            container_name=container_name,
            blob_name=dim_file_name,
            overwrite=True,
        )
    logging.info(f"Successfully Loaded Dim Openings to Azure Storage as {dim_file_name}")

    # Install azure extension


def load_dim_date(**kwargs):
    """Loads and transforms the dim_date dimension table from fact table in silver layer.
    Extracts all the dates and appends them to an existing dimensional date table 
    or creates a new one if it doesn't exist.
    Args:
        **kwargs: Keyword arguments passed from the Airflow task, 
        including task instance (ti).
    """    
    
    # Extract filename from airflow xcoms from previous run
    ti = kwargs["ti"]
    filename = ti.xcom_pull(
        task_ids="transform_json_to_fact_table",
        dag_id="pull_data_from_chess_api",
        key="fact_blob_name",
    )
    # filename  = kwargs['dim_destination']
    logging.info(f"Received File Name: {filename}")
    con = initialize_azure_extension()
    dim_file_name = "gold/dim_date.parquet"

    source_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{filename}"
    destination_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{dim_file_name}"
    # Check existence of file in Azure storage
    file_exists = az_hook.check_for_blob(container_name=container_name, 
                                         blob_name=dim_file_name)

    if file_exists:
        dim_date = con.sql(
            f"""
                SELECT DISTINCT game_date,
                        EXTRACT(YEAR FROM game_date) AS year,
                        EXTRACT(MONTH FROM game_date) AS month, 
                        strftime('%B', game_date) AS month_name,
                        EXTRACT(DAY FROM game_date) AS day,
                        strftime('%A', game_date) AS weekday,
                        CASE 
                            WHEN CAST(strftime('%m', game_date) AS INTEGER) BETWEEN 1 AND 3 THEN 1
                            WHEN CAST(strftime('%m', game_date) AS INTEGER) BETWEEN 4 AND 6 THEN 2
                            WHEN CAST(strftime('%m', game_date) AS INTEGER) BETWEEN 7 AND 9 THEN 3
                            ELSE 4 END AS quarter
                            
                FROM '{source_file_path}' 
                WHERE game_date NOT IN 
                
                ( SELECT game_date FROM '{destination_file_path}')
                UNION
                SELECT * FROM '{destination_file_path}';
                
                """
        )

    else:
        dim_date = con.sql(
            f""" SELECT DISTINCT game_date,
                        EXTRACT(YEAR FROM game_date) AS year,
                        EXTRACT(MONTH FROM game_date) AS month,
                        strftime('%B', game_date) AS month_name,
                        EXTRACT(DAY FROM game_date) AS day,
                        strftime('%A', game_date) AS weekday,
                        CASE 
                            WHEN CAST(strftime('%m', game_date) AS INTEGER) BETWEEN 1 AND 3 THEN 1
                            WHEN CAST(strftime('%m', game_date) AS INTEGER) BETWEEN 4 AND 6 THEN 2
                            WHEN CAST(strftime('%m', game_date) AS INTEGER) BETWEEN 7 AND 9 THEN 3
                            ELSE 4 END AS quarter
                FROM '{source_file_path}'
                ORDER BY game_date; 
    """
        )

    # Upload the file to gold layer
    upload_duckdb_to_azure(dim_date, container_name, dim_file_name)


def load_dim_time_control(**kwargs):
    """Loads and transforms the dim_time_control dimension table from the fact table 
    in the silver layer, and uploads the result to the gold layer in Azure storage.

    Args:
        **kwargs: Arbitrary keyword arguments, including:
            - ti: Task instance object for accessing Airflow XComs.

    Returns:
        None
    """
    
    # Extract filename from airflow xcoms from previous run
    ti = kwargs["ti"]
    filename = ti.xcom_pull(
        task_ids="transform_json_to_fact_table",
        dag_id="pull_data_from_chess_api",
        key="fact_blob_name",
    )
    logging.info(f"Received File Name: {filename}")
    con = initialize_azure_extension()

    dim_file_name = "gold/dim_time_control.parquet"
    source_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{filename}"
    destination_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{dim_file_name}"
    # Check existence of file in Azure storage
    file_exists = az_hook.check_for_blob(
        container_name=container_name, blob_name=dim_file_name
    )

    if file_exists:
        dim_time_control = con.sql(
            f"""
                SELECT DISTINCT format_time_control(time_control) as time_control, time_class 
                FROM '{source_file_path}' WHERE time_control NOT IN 
                ( SELECT time_control FROM '{destination_file_path}')
                UNION 
                SELECT * FROM '{destination_file_path}';
                """
        )

    else:
        dim_time_control = con.sql(
            f"""SELECT format_time_control(time_control) as time_control, time_class
                FROM '{source_file_path}';
    """
        )

    # Upload the file to gold layer
    upload_duckdb_to_azure(dim_time_control, container_name, dim_file_name)


def load_dim_results(**kwargs):
    """Updates or creates the 'dim_results' dimension table in the datalake Gold layer.
    Args:
        **kwargs: Arbitrary keyword arguments, including:
            - ti: Task instance object for accessing Airflow XComs.
    Returns:
        None
    """
    # Extract filename from airflow xcoms from previous run
    ti = kwargs["ti"]
    filename = ti.xcom_pull(
        task_ids="transform_json_to_fact_table",
        dag_id="pull_data_from_chess_api",
        key="fact_blob_name",
    )

    # filename  = kwargs['dim_destination']
    logging.info(f"Received File Name: {filename}")
    con = initialize_azure_extension()
    dim_file_name = "gold/dim_results.parquet"

    destination_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{dim_file_name}"
    # Check existence of file in Azure storage
    file_exists = az_hook.check_for_blob(
        container_name=container_name, blob_name=dim_file_name
    )
    if file_exists:
        dim_results = con.sql(f"SELECT * FROM '{destination_file_path}'")

    else:
        dim_results = con.sql(
            """
                SELECT 'win' AS result_code, 'Win' AS result, 'Win' AS description
        UNION ALL
        SELECT 'checkmated', 'Loss', 'Checkmated'
        UNION ALL
        SELECT 'agreed', 'Draw', 'Draw agreed'
        UNION ALL
        SELECT 'repetition', 'Draw', 'Draw by repetition'
        UNION ALL
        SELECT 'timeout', 'Win', 'Timeout'
        UNION ALL
        SELECT 'resigned', 'Loss', 'Resigned'
        UNION ALL
        SELECT 'stalemate', 'Draw', 'Stalemate'
        UNION ALL
        SELECT 'lose', 'Loss', 'Lose'
        UNION ALL
        SELECT 'insufficient', 'Draw', 'Insufficient material'
        UNION ALL
        SELECT '50move', 'Draw', 'Draw by 50-move rule'
        UNION ALL
        SELECT 'abandoned', 'Draw', 'Abandoned'
        UNION ALL
        SELECT 'kingofthehill', 'Win', 'Opponent king reached the hill'
        UNION ALL
        SELECT 'threecheck', 'Win', 'Checked for the 3rd time'
        UNION ALL
        SELECT 'timevsinsufficient', 'Draw', 'Draw by timeout vs insufficient material'
        UNION ALL
        SELECT 'bughousepartnerlose', 'Loss', 'Bughouse partner lost'
        """
        )

        upload_duckdb_to_azure(dim_results, container_name, dim_file_name)


def load_fact_table(**kwargs):
    """Loads and updates the fact table in the gold layer of the data pipeline.
    This function fetches transformed data from the silver layer, processes it to 
    generate a fact table, and uploads the updated fact table to Azure Blob Storage. 
    It ensures deduplication by retaining the most recent records based on the 
    `last_updated` timestamp.
    Args:
        **kwargs: Arbitrary keyword arguments, including:
            - ti: Task instance for XCom communication.
            - exec_date: Execution date of the DAG run.
    Returns:
        None
        """

    # Fetch the monthly data from the silver layer.
    ti = kwargs["ti"]
    filename = ti.xcom_pull(
        task_ids="transform_json_to_fact_table",
        dag_id="pull_data_from_chess_api",
        key="fact_blob_name",
    )
    exec_date = kwargs["exec_date"]
    logging.info(exec_date)
    # exec_date = datetime.strptime(kwargs['exec_date'], "%Y-%m-%d").date()

    logging.info(f"Received File Name: {filename}")
    # Initailize azure and duckdb instance
    con = initialize_azure_extension()

    # Define Source and destinations
    fact_file_name = "gold/fact-games.parquet"
    source_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{filename}"
    destination_file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{fact_file_name}"
    # Check existence of file in Azure storage
    file_exists = az_hook.check_for_blob(container_name=container_name, blob_name=fact_file_name)

    # Create references for all dimensional files
    dim_openings = "az://rbchesssa.blob.core.windows.net/chess-etl-files/gold/dim_openings.parquet"
    dim_date = "az://rbchesssa.blob.core.windows.net/chess-etl-files/gold/dim_date.parquet"
    dim_results = "az://rbchesssa.blob.core.windows.net/chess-etl-files/gold/dim_results.parquet"
    dim_time_control = "az://rbchesssa.blob.core.windows.net/chess-etl-files/gold/dim_time_control.parquet"

    fct = con.sql(
        f"""
            SELECT game_url as game_url,
                game_date as game_date,
                start_time as start_time,
                end_time as end_time,
                date_diff('seconds', start_time, end_time)::BIGINT as game_duration_secs,
                format_time_control(time_control) as time_control, 
                CASE WHEN pgn_white_user = 'Rhythmbear1' THEN 'white'
                    ELSE 'black' END as my_color,
                CASE WHEN pgn_white_user = 'Rhythmbear1' THEN pgn_white_user
                    ELSE pgn_black_user END as my_username,
                CASE WHEN pgn_white_user = 'Rhythmbear1' THEN pgn_black_user
                    ELSE pgn_white_user END as opponent_username,
                CASE 
                    WHEN pgn_white_user = 'Rhythmbear1' THEN white_rating
                    ELSE black_rating END as my_rating,
                CASE 
                    WHEN pgn_white_user = 'Rhythmbear1' THEN black_rating
                    ELSE white_rating END as opponent_rating,
                CASE 
                    WHEN pgn_white_user = 'Rhythmbear1' THEN white_result
                    ELSE black_result END as my_result,
                CASE 
                    WHEN pgn_white_user = 'Rhythmbear1' THEN black_result
                    ELSE white_result END as opponent_result,
                pgn_current_position as game_fen,
                pgn_eco_url as opening_url,
                pgn_trans as game_pgn,
                get_pgn_depth(pgn_trans) as moves,
                '{exec_date}'::TIMESTAMP as last_updated
            FROM '{source_file_path}' as fact""")

    fact_table = con.sql(
        f"""SELECT fact.* FROM fct AS fact
                LEFT JOIN '{dim_date}' AS dim_date ON fact.game_date = dim_date.game_date
                LEFT JOIN '{dim_openings}' AS dim_openings ON fact.opening_url = dim_openings.pgn_eco_url
                LEFT JOIN '{dim_results}' AS dim_results_my ON fact.my_result = dim_results_my.result_code
                LEFT JOIN '{dim_results}' AS dim_results_op ON fact.opponent_result = dim_results_op.result_code
                LEFT JOIN '{dim_time_control}' AS dim_time_control ON fact.time_control = dim_time_control.time_control;
            """)
    logging.info(f"fact table to be added has: {fact_table.shape}")

    if file_exists:
        prev_fact_table = con.sql(f"""SELECT * FROM '{destination_file_path}' """)
        logging.info(f"The Previous fact table has : {prev_fact_table.shape}")  # Should not be (0, 0)

        both_tables = con.sql(f"""SELECT * FROM prev_fact_table
                                    UNION ALL 
                                    SELECT * FROM fact_table;""")
        logging.info(f"Both Tables have: {both_tables.shape}")
        new_fact_table = con.sql(
            f"""SELECT * 
                FROM (
                    SELECT *, 
                            ROW_NUMBER() OVER ( PARTITION BY game_url ORDER BY last_updated DESC) AS rn
                            FROM both_tables
                    )
                WHERE rn = 1;
                """
        ).fetchdf()

        # Here i'm droping the row number column and converting the query back to a Duckdb.Pyrelation Object so that i can
        # pass it into the upload duckdb to azure function.
        new_fact_table.drop(columns=["rn"], inplace=True)
        new_fact_table = con.from_df(new_fact_table)

    else:
        new_fact_table = fact_table

    upload_duckdb_to_azure(new_fact_table, container_name, fact_file_name)


#################################### LOAD TO DATABASES ##############################################


def load_fact_to_postgres(**kwargs):
    """Loads fact data from datalake gold layer
      into the datawarehouse.

    Args:
        **kwargs: Arbitrary keyword arguments, including:
            - ti: Task instance object for accessing XComs.

    Returns:
        None
    """
    ti = kwargs["ti"]
    last_updated_date = ti.xcom_pull(
        task_ids="get_last_updated_date",
        dag_id="load_data_warehouse",
        key="return_value",
    )[0][0]
    logging.info(f"Fetching data for Last Updated Date: {last_updated_date}")
    con = initialize_azure_extension()

    fact = con.sql(
        f"""
            SELECT * 
            FROM 'az://rbchesssa.blob.core.windows.net/chess-etl-files/gold/fact-games.parquet'
        """
    ).df()

    logging.info(f"✅ DataFrame Loaded: {fact.shape[0]} rows, {fact.shape[1]} columns")
    engine = psql_hook.get_sqlalchemy_engine()

    fact.to_sql(
        name="fact_games",
        schema="chess_dw",
        con=engine,
        if_exists="replace",
        index=False,
    )
    logging.info("Successfully Loaded Data to Postgres")


def load_dim_table_to_postgres(dim_file_name: str, table_name: str):
    """
    A template for loading dimensional data from 
    datalake gold layer into the datawarehouse.

    Args:
        **kwargs: Arbitrary keyword arguments, including:
            - ti: Task instance object for accessing XComs.

    Returns:
        No
    """
    con = initialize_azure_extension()
    file_path = f"az://rbchesssa.blob.core.windows.net/chess-etl-files/{dim_file_name}"
    dim = con.sql(
        f"""
            SELECT * 
            FROM '{file_path}'
            """
    ).df()

    logging.info(f"✅ {dim_file_name} Loaded: {dim.shape[0]} rows, {dim.shape[1]} columns")
    engine = psql_hook.get_sqlalchemy_engine()

    dim.to_sql(
        name=table_name, schema="chess_dw", con=engine, if_exists="replace", index=False
    )
    logging.info(f"Successfully Loaded {dim_file_name} Data to Postgres table {table_name}")
