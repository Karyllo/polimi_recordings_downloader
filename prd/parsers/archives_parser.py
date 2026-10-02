from datetime import datetime
from itertools import repeat
from multiprocessing.pool import ThreadPool
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse
import requests
import re
from bs4 import BeautifulSoup, Tag
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from prd.utils import extract_academic_year_from_datetime
from prd.parsers import Parser
from prd.webex_api import Recording, extract_id_from_url, generate_recording_from_id


class ArchivesParser(Parser):
    """Class to parse archives pages."""

    # L'archivio si è spostato da www11.ceda.polimi.it a onlineservices.polimi.it (cookie JSESSIONID + INGRESSCOOKIE)
    ALLOWED_HOSTS = ("www11.ceda.polimi.it", "onlineservices.polimi.it")
    PATH_PREFIX = "/recman_frontend/recman_frontend/controller/"

    def __init__(
        self,
        cookie_ticket: str,
        cookie_SSL_JSESSIONID: Optional[str] = None,
        cookies: Optional[Dict[str, str]] = None,
    ):
        """Create the parser.

        Args:
            cookie_ticket (str): The ticket cookie.
            cookie_SSL_JSESSIONID (str): The SSL_JSESSIONID cookie (old www11.ceda host).
            cookies (dict): Recman cookies, e.g. {"JSESSIONID": ..., "INGRESSCOOKIE": ...} (new host).
        """
        self.cookie_ticket = cookie_ticket
        self.cookies: Dict[str, str] = dict(cookies or {})
        if cookie_SSL_JSESSIONID:
            self.cookies["SSL_JSESSIONID"] = cookie_SSL_JSESSIONID

    def parse(self, url: str) -> List[Recording]:
        """Parse an url of the recording archives.

        Args:
            url (str): The url of the recording archives.

        Raises:
            ValueError: If the url is not correct.
            RuntimeError: If no recordings are found in the page.

        Returns:
            List[Recording]: The list of recordings.
        """
        # Option check
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc not in self.ALLOWED_HOSTS \
                or not parsed.path.startswith(self.PATH_PREFIX):
            raise ValueError(
                "The url must start with https://<host>" + self.PATH_PREFIX
                + " with host one of: " + ", ".join(self.ALLOWED_HOSTS)
            )

        soup: BeautifulSoup = self._get_soup(url)
        # La tabella è paginata (10 per pagina): se c'è il link "tutte" (action=plen_0) si carica quello
        all_link = next((a["href"] for a in soup.select("a.paginator_link") if "action=plen_0" in a.get("href", "")), None)
        if all_link:
            soup = self._get_soup(urljoin(url, all_link))
        rows: List[Tag] = soup.select("tbody.TableDati-tbody tr")

        if len(rows) == 0:
            raise RuntimeError(
                "Zero recordings were found, make sure the recman cookies are correct "
                "(JSESSIONID and INGRESSCOOKIE for onlineservices.polimi.it, SSL_JSESSIONID for www11.ceda.polimi.it)."
            )
        print(f"There are {len(rows)} rows in the page")
        self._base_url = url

        # Need to work for both UserListActivity.do and ArchivioListActivity.do
        is_UserListActivity: bool = parsed.path.startswith(self.PATH_PREFIX + "UserListActivity.do")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
        ) as progress:
            progress.add_task(
                description="Generating recording download links...", total=None
            )
            pool: ThreadPool = ThreadPool()
            recordings: List[Recording] = pool.starmap(
                self._generate_recording_from_row,
                zip(rows, repeat(is_UserListActivity)),
            )
        
        return recordings

    def _generate_recording_from_row(
        self, row: Tag, is_UserListActivity: bool
    ) -> Recording:
        """Create a Recording object from a row of the recordings table.

        Args:
            row (Tag): Row of the recordings table.
            is_UserListActivity (bool): If the row is from a UserListActivity page.

        Returns:
            Recording: Generated recording object.
        """
        cells = row.select("td")

        video_url: str = self._get_video_url_from_recman_redirection_link(
            urljoin(self._base_url, cells[0].select_one("a.Link")["href"])   # link relativi (vecchio) o assoluti (nuovo)
        )
        video_id: str = extract_id_from_url(video_url, ticket=self.cookie_ticket)

        try:
            if not is_UserListActivity:
                date_str = cells[2].text.strip()
                recording_datetime: datetime = datetime.strptime(
                    date_str, "%d/%m/%Y %H:%M"
                )
                course: str = cells[3].text
                subject: str = cells[5].text
                academic_year: str = cells[1].text.replace(" / ", "-")
            else:
                date_str = cells[1].text.strip()
                recording_datetime: datetime = datetime.strptime(
                    date_str, "%d/%m/%Y %H:%M"
                )
                course: str = cells[2].text.replace("\n", " ")
                # "[Forma didattica] argomento": la forma (Lezione/Laboratorio/Esercitazione/Altro) serve a chi
                # rinomina i file per tipo (sbob la legge dalla colonna Subject dell'xlsx)
                forma: str = cells[3].text.strip()
                subject: str = (f"[{forma}] " if forma else "") + cells[4].text.replace("\n", " ").strip()
                academic_year = extract_academic_year_from_datetime(recording_datetime)
        except ValueError:
            # Se la data non è valida, usa la data attuale come segnaposto e continua
            print(f"\n!!! ATTENZIONE: Trovata una data non valida ('{date_str}'), verrà usata la data attuale come segnaposto.\n")
            recording_datetime = datetime.now()
            if not is_UserListActivity:
                course: str = cells[3].text
                subject: str = cells[5].text
                academic_year: str = cells[1].text.replace(" / ", "-")
            else:
                course: str = cells[2].text.replace("\n", " ")
                subject: str = cells[4].text.replace("\n", " ")
                # Non possiamo estrarre l'anno da una data non valida, usiamo un segnaposto
                academic_year = "ANNO-SCONOSCIUTO"

        course: str = course.replace("\n", " ")
        subject: str = subject.replace("\n", " ")

        recording: Recording = generate_recording_from_id(
            video_id=video_id,
            ticket=self.cookie_ticket,
            academic_year=academic_year,
            recording_datetime=recording_datetime,
            course=course,
            subject=subject,
        )

        return recording

    def _get_soup(self, url: str) -> BeautifulSoup:
        res: requests.Response = requests.get(url, cookies=self.cookies)
        return BeautifulSoup(res.content, "html.parser")

    def _get_video_url_from_recman_redirection_link(self, link: str) -> str:
        """Get the video url from the link of the redirection to the recording.

        Args:
            link (str): Link of recman redirection to the recording.

        Returns:
            str: The url of the video.

        Raises:
            RuntimeError: If unable to extract url from redirection link.
        """
        res = requests.get(link, cookies=self.cookies)
        id_search = re.search(
            r"location\.href='(.*)';",
            res.text,
        )
        if not (id_search):
            raise RuntimeError(
                "Was not able to extract video url from recman redirection link. Retry."
            )
        return id_search.group(1)
