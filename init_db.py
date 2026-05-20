from dotenv import load_dotenv

from db_schema import init_database


def main():
    load_dotenv()
    init_database()


if __name__ == "__main__":
    main()
