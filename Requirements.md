Assign yourself as Lead Data Engineer,
Help me to build a Bid Analyzer tool for onepoint and make quick decsions on the tenders 
Tool stack : Python

Approach is detailed here 

1. Read the tenders from the spreadsheet referred in project_config.json 

2. Run thorugh the tenders one by one and pass the tender title and description to the analyzer module 
and record the response from the analyzer module back in the [Bid Qualification],	[Bid Qualification Reason], [Bid Qualification Date] as detailed below,
    a. [Bid Qualification] = update the response status from the Analyzer module ( Bid/NoBid/TBD )
    b. [Bid Qualification Reason] = Record the reason received from the analyser tool, mention this is a system genrerated response.
    c. [Bid Qualification Date] = date on which the process was executed
    
    Finaly it should process all the tenders and it should also append the relative comments in the comments column, and re-use the logic to update the following control columns  Processed Date, Last Modified Date, Created Date

3. The Analyser Module should take inputs title and description fom the main module, and perform the  operation
    by analyzing the capability of Onepoint to meet the tender requirements by vetting it  against the following onepoint's  documents and give a detailed in Bid/NoBid Analysis score out of 100,  
Context for the Analyzer Module is detailed here    
    https://notebooklm.google.com/notebook/8d3b7ac1-7a0b-4812-ab4b-d0fe551fe7cd
    https://notebooklm.google.com/notebook/d715df10-63b4-4bf5-8b6e-7f03e08da62e
    https://notebooklm.google.com/notebook/fa8c929f-1ac2-4765-8f05-30954180cc25
    https://notebooklm.google.com/notebook/29f7c706-3a45-4231-93ee-839c20c87afd 

Analyzer module should use model="google/gemini-3.5-flash" via the openrouter as it is approached in keyword_search.py 
Analyser should act as Tender Analyst for Onepoint and perform this operation sincerely to give us hope towards the tenders to be marked for Bidding excercise.

The output of the analyser should be these 3 values should bethe follwing values,
    1. Bid_Qualification : Bid/NoBid/TBD {  Bid : if the Analysis score falls above 75, 
                                     TBD : if the Analysis score falls between 51-75,
                                     NoBid : if the Analysis score falls below 50}
    2. Bid_Qualification_Reason : Summarised reason based onthe Analysis performed by the analyzer 
    3. Bid_Qualification_Date : Date on which the qulification is arrived 
these 3 arguements returned will be used by the main module to update the respective columns as detailed in Step 2 




  
