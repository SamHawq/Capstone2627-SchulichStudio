# Project Goal
The project aims to develop an Agentic AI solution for ranking Teaching Assistants (TA) applicants for first-year engineering courses. The system will consist of multiple specialized AI agents that collaborate during the TA selection process. These agents will be designed to account for professor-specific selection criteria and course requirements, assist in identifying and mitigating potential bias, support diversity in selections, and provide an easier experience for Professors in the TA selection process. The solution should work for both Mac and Windows OS, and potentially smartphone use. The preferred platform for development is VSCode, GitHub, Python, and the OpenAI Agent SDK. Python will be used to implement the backend logic, integrate the agents, access external services and databases, and expose REST API endpoints for communication between frontend and the agent system. The OpenAI Agent SDK will be used to define and orchestrate the agents, including their instructions, tools, communication, handoffs, and workflows. During the development we will be evaluating different Large Language Models to determine which are most appropriate for the individual agents. The agents on our list are the OpenAI GPT models and Anthropic Claude models. We will be considering factors such as response quality, accuracy, latency, token usage, and cost. Non OpenAI models can be integrated into the Agents SDK. Currently the projects existing applicant data has been provided by the project sponsor and is stored in a MySQL relational database. Agents will be provided with API functions that allow them to retrieve relevant information from the database, such as TA applications, previous selections, and course information. We will also be looking into the use of a vector database and Retrieval-Augmented Generation (RAG) for storing and retrieving information about selection guidelines, historical selection information, and course requirements. This should allow the agents to access both structured applicant data and relevant contextual information when it produces its recommendations. 
Note* While this is the goal to appease the sponser, our solution should be dynamic enough that our solution could be provided to any client wishing to optimize hiring.
<!--
  The "why" of the project. Should rarely change once agreed on with your team/sponsor.
  Anyone reading only this file should understand what problem you're solving and what success looks like.
-->

## Problem Statement
TA information is currently collected through surveys, emails and other resources. They are then entered into an excel spreadsheet. The hiring process is performed by searching the records in the spreadsheets. This process is time consuming, can delay assignments, and is difficult to apply the same reasoning consistently and introduces risks of accidental modifications and loss of data (data in the spreadsheets). While a framework to observe TA availability helps simplify this task to a degree, the professor still has to do a lot of work just to determine if a TA is an adequate fit for the course and the requirements of the role. A machine learning model that takes in many factors such as experience, qualifications, reviews, etc. can simplify this task for Professors and support them in easily making an informed choice when selecting candidates. It can be further strengthened to factor in ethnicity and sex to provide a diverse selection of TAs per section. 
<!-- What problem exists today, and for whom? (e.g. manually ranking TA candidates is slow/inconsistent.) -->

## Objective
An API clients can access that will trigger an ecosystem of agents collaborating to deliver an assessed, informed, and helpful ranking of candidates that the client can easily display to their users after our system sifts through obtained data.
<!-- One or two sentences on what this project will deliver to solve that problem. -->

## Stakeholders / Users
End users: People looking to choose strong candidates among many like Professors hiring TAs.
Its being built for capstone sponsor that wants us to develop their process of professors hiring TAs (While our solution aims to accomplish this, while being suitable for other clients)
Team: Adapt Software
<!-- Who uses it (end users), who it's built for (sponsor/client), and who's on the team. -->

## Scope
### In Scope
- A REST API that accepts a hiring request (formatted criteria plus optional weights and free-text instructions) and returns a ranked list of candidates with explanations
- A multi-agent system built on the OpenAI Agents SDK: Manager/Orchestrator, Criteria Formatter, criterion-based Ranker Agents, and the Scheduler
- A Criterion Registry with a standard interface, including bonus slots for client-defined criteria
- Handling of hard constraints (exclude candidates) and soft constraints (adjust ranking), such as availability against course schedules
- Agent tools for reading the sponsor's MySQL data (applications, previous selections, course info)
- Investigating a vector database / RAG for selection guidelines, historical selections and course requirements
- Comparing LLMs (OpenAI GPT and Anthropic Claude) per agent on quality, accuracy, latency, token usage and cost
- Removing or hashing sensitive identifiers before data is sent to an LLM
- A generic design that can be reused for hiring problems beyond TA selection

### Out of Scope
<!-- Confirm these with the team and sponsor. They are reasonable defaults, not decisions. -->
- Making final hiring decisions. The system recommends; the professor decides.
- Training or fine-tuning a custom ML model
- Replacing the sponsor's data collection (surveys, emails), beyond reading the data they provide
- Writing back to or changing the sponsor's database (read-only access)
- A full production frontend. Clients display the API results in their own UI. A demo UI is **TBD**.
- Native mobile apps. Mobile support means the API works with a mobile-friendly client.
- HR/payroll processes after selection (contracts, onboarding)

## Success Criteria
<!-- Fill in the targets with the sponsor. The numbers below are placeholders. -->
- **Functional:** an end-to-end API call returns a ranked, explained list of candidates for a real course using sponsor data
- **Agreement with experts:** the system's top-X overlaps with professors' or historical selections at least **TBD**% of the time on a test set
- **Constraint correctness:** no candidate who fails a hard constraint appears in the final ranking (0 violations on test cases)
- **Consistency:** repeated runs on the same input give stable rankings (variation within **TBD**)
- **Explainability:** every ranked candidate has a short, evidence-based rationale per criterion
- **Performance and cost:** one request finishes in under **TBD** seconds and costs under **TBD** in LLM usage
- **Privacy:** no direct identifiers (e.g. UCID) are present in any LLM prompt (checked by logging or tests)
- **Time saved:** professors report less time spent on TA selection than with the spreadsheet process
- **Generality:** at least one non-TA hiring scenario runs by changing only the criteria/registry, not the code
- **Sponsor sign-off** at the Fall, Winter and Final evaluations

## Constraints & Assumptions
**Constraints**
- Tech stack: Python, OpenAI Agents SDK, VSCode, GitHub, as the sponsor prefers
- Must work on Mac and Windows; smartphone use through the client is a possible extra
- Timeline: the 2026–27 capstone year, with Fall, Winter and Final evaluations (exact dates **TBD**)
- Budget: LLM API usage costs (budget and who pays are **TBD**)
- Privacy: applicant data is personal information, so university and provincial privacy rules apply to storing it and sending it to third-party LLMs
- Fairness: human-rights and university hiring policies limit how demographic data may be used

**Assumptions**
- The sponsor's MySQL database is available to the team and its data is accurate enough to rank on
- Clients can give criteria in our format, or accept and review output from the Criteria Formatter Agent
- Professors will review the recommendations rather than accept them automatically
- Non-OpenAI models (e.g. Claude) can be integrated through the Agents SDK when needed
