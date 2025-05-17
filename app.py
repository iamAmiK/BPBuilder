from flask import Flask, render_template, request
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
from pymongo import MongoClient
import os
import zipfile
import json
import shutil 
import subprocess
from pathlib import Path

app = Flask(__name__)
client = MongoClient('localhost', 27017)
db = client.apirepository
apis = db.apis
inputs = db.inputs
outputs = db.outputs
bps = db.business_processes
brs = db.business_rules

# Home Route
@app.route('/')
def home():
    return render_template('home.html')

# Funtion that generates the AWS Package which contains
# - JSON for policy with required permissions
# - JSON for role with policy
# - zips for Lambda Functions
# - JSON for Step Functions.
# - Instructions for setting up the step functions.
@app.route("/results", methods=["POST"])
def generate_step_function_results():
    # Get the API names.
    api_names_str = request.form.get("apis", "")
    api_names = [name.strip() for name in api_names_str.split(",") if name.strip()]
    if not api_names:
        return "No APIs selected."

    # Deployment package - this is folder to be zipped as AWS Package.
    BASE_DIR = Path("deployment_package")
    LAMBDAS_DIR = BASE_DIR / "lambda_zips"
    LAMBDAS_DIR.mkdir(parents=True, exist_ok=True)

    # Add triggerstepfunctions Lambda function to trigger the step functions
    trigger_code = """\
    import json
    import boto3
    import base64

    def lambda_handler(event, context):
        s3 = boto3.client('s3')
        bucket = event['Records'][0]['s3']['bucket']['name']
        key = event['Records'][0]['s3']['object']['key']
        destination_bucket = 'base64pdf'
        destination_key = key[:-4]

        file = s3.get_object(Bucket=bucket, Key=key)
        file_content = base64.b64encode(file['Body'].read()).decode('utf-8')

        s3.put_object(
            Bucket = destination_bucket,
            Key = destination_key,
            Body = file_content,
            ContentType = 'text/plain'
        )

        info = {
            'bucket': destination_bucket,
            'key': destination_key
        }
        
        sf = boto3.client('stepfunctions', region_name='ap-southeast-2')

        response = sf.start_execution(
            stateMachineArn = 'arn:aws:states:ap-southeast-2:600627320862:stateMachine:UpbrainsRishabhESSValidator',
            input = json.dumps(info)
        )
        
        return {
            'statusCode': 200,
            'body':  json.dumps(response, default=str)
        }
    """

    dir = BASE_DIR / f"lambda_triggerstepfunctions"
    dir.mkdir(parents=True, exist_ok=True)

    handler = dir / "lambda_handler.py"
    handler.write_text(trigger_code)

    lambda_zip_paths = []

    # Zip lambda folder
    zip_path = LAMBDAS_DIR / f"triggerstepfunctions.zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(dir):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, start=dir)
                zipf.write(filepath, arcname)

    lambda_zip_paths.append(zip_path)
    shutil.rmtree(dir)
    
    for api in api_names:

        # Locate the API document in MongoDB
        doc = apis.find_one({"API Name": api})
        if not doc:
            return f"No data found for API: {api}"

        # Get the Lambda code and the module requirements for each API
        lambda_code = doc.get("Lambda Code", "")
        requirements = doc.get("Requirements", [])

        # Make the directory for the API's Lambda ZIP
        api_dir = BASE_DIR / f"lambda_{api}"
        api_dir.mkdir(parents=True, exist_ok=True)

        # Write lambda_handler.py for each API, using code extracted from MongoDB
        handler_path = api_dir / "lambda_handler.py"
        handler_path.write_text(lambda_code)

        # Install dependencies - most likely just requests.
        for requirement in requirements:
            subprocess.run(["pip", "install", requirement, "-t", str(api_dir)], check=True)

        # Zip lambda folder
        zip_path = LAMBDAS_DIR / f"{api}.zip"
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(api_dir):
                for file in files:
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, start=api_dir)
                    zipf.write(filepath, arcname)

        lambda_zip_paths.append(zip_path)
        shutil.rmtree(api_dir)

    # Write instructions and config files
    (BASE_DIR / "instructions.txt").write_text("""\
        Instructions: Setting Up AWS Step Functions and Lambda Functions

        This guide will walk you through deploying and connecting AWS Lambda functions and Step Functions using the contents of the deployment package you downloaded.

        1. Unpack the Deployment Package
        - Locate the ZIP file you downloaded.
        - Unzip the contents into a working directory on your local machine.

        2. Sign In to AWS Console
        - Navigate to https://aws.amazon.com/console.
        - Log in as the root user with your AWS credentials.

        3. Set Up Required S3 Buckets
        - Go to S3 in the AWS Console.
        - Create a bucket named: triggerstepfunctions.
        - Create another bucket named: base64pdf.
        - Copy the ARN of the base64pdf bucket from its properties.

        4. Create an IAM Policy
        - Navigate to IAM > Policies > Create Policy.
        - Select the JSON tab and paste the contents of iam_policy.json from the package.
        - Name the policy (e.g., StepFunctionPolicy).
        - Create the policy and copy its ARN.

        5. Create an IAM Role
        - Go to IAM > Roles > Create Role.
        - Choose AWS service > Lambda as the trusted entity type.
        - Skip permission assignment.
        - Open rule_with_policy_reference.json and replace the policy ARN placeholder with the one copied earlier.
        - Paste the edited JSON into the Trust Policy Editor.
        - Name the role (e.g., StepFunctionExecutionRole) and create it.

        6. Deploy Lambda Functions

        A. Deploy triggerstepfunctions Lambda
        - Go to Lambda > Create Function.
        - Author from scratch, name it triggerstepfunctions, and select Python as runtime.
        - Attach the IAM role created earlier.
        - Upload lambda_zips/triggerstepfunctions.zip in the Code section.

        B. Add Trigger to Lambda
        - Go to Configuration > Triggers.
        - Add an S3 trigger for triggerstepfunctions bucket on ObjectCreated events.

        C. Update Lambda Code
        - Open lambda_handler.py and replace the destination bucket ARN placeholder with the ARN of base64pdf.

        D. Deploy Remaining API Lambdas
        - Repeat the function creation steps for each remaining zip in lambda_zips/.
        - Upload, assign the IAM role, and deploy each one.

        7. Create AWS Step Function
        - Go to Step Functions > Create State Machine.
        - Use the code editor and paste step_function_definition.json.
        - Replace Lambda ARN placeholders with actual function ARNs.
        - Select the IAM role created earlier and deploy.

        Final Checklist
        - [ ] S3 buckets created (triggerstepfunctions, base64pdf)
        - [ ] IAM Policy created and ARN copied
        - [ ] IAM Role created and assigned the policy
        - [ ] Lambda functions deployed with correct code and IAM Role
        - [ ] Trigger configured for triggerstepfunctions Lambda
        - [ ] Step Function JSON updated with correct ARNs
        - [ ] State Machine deployed and connected to role
        """
    )

    # Writing the structure for step functions API Lambda invocations in JSON
    # ARNs to be replaced by the user manually when setting up Step Functions.
    states = {}
    for i, api in enumerate(api_names):
        state_name = f"Step{i+1}_{api}"
        states[state_name] = {
            "Type": "Task",
            "Resource": f"arn:aws:lambda:REGION:ACCOUNT_ID:function:lambda_{api}",
            "Next": f"Step{i+2}_{api_names[i+1]}" if i+1 < len(api_names) else "EndState"
        }

    # Put the endstate of success for the Step Functions
    if api_names:
        states["EndState"] = {"Type": "Succeed"}

    # Defining the initial metadata for the Step Functions.
    (BASE_DIR / "step_function_definition.json").write_text(json.dumps({
        "Comment": "Generated Step Function",
        "StartAt": f"Step1_{api_names[0]}" if api_names else "",
        "States": states
    }, indent=4))

    # Writing the permissions for the iam_policy
    # Includes permissions to modify S3 buckets - required to communicate with the buckets
    # Also includes permissions to start and stop the execution of step functions!
    (BASE_DIR / "iam_policy.json").write_text(json.dumps({
        "Version": "2025-04-29",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
                "Resource": "arn:aws:s3:::example-bucket/*"
            },
            {
                "Effect": "Allow",
                "Action": ["states:StartExecution", "states:DescribeExecution", "states:StopExecution"],
                "Resource": "*"
            }
        ]
    }, indent=4))

    # Writing the JSON that defines the IAM role to be added to add Lambda Functions + Step Functions
    (BASE_DIR / "rule_with_policy_reference.json").write_text(json.dumps({
        "Name": "StepFunctionTriggerRule",
        "EventPattern": {"source": ["aws.events"]},
        "State": "ENABLED",
        "Targets": [{
            "Id": "TargetFunction",
            "Arn": "arn:aws:iam::ACCOUNT_ID:policy/YourPolicyName"
        }]
    }, indent=4))

    # Final bundle
    output_filename = "AWSPackage.zip"
    with zipfile.ZipFile(output_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for zip_path in lambda_zip_paths:
            zipf.write(zip_path, arcname=f"lambda_zips/{zip_path.name}")
        for filename in ["instructions.txt", "step_function_definition.json", "iam_policy.json", "rule_with_policy_reference.json"]:
            zipf.write(BASE_DIR / filename, arcname=filename)

    # Move the zip to the static folder for the HTML to pick up.
    shutil.move(output_filename, Path("static") / output_filename)

    # Deleting the directory after it has been zipped.
    shutil.rmtree(BASE_DIR)

    return render_template("results.html") # send_file(output_filename, as_attachment=True)

# API Selection Route
@app.route('/selected_bp', methods=['GET' ,'POST'])
def select_apis():

    # Get the selected APIs as a Strings
    apis_str = request.form.get('apis', '')

    # Convert string to list of APIs
    api_list = [api.strip() for api in apis_str.split(',') if api.strip()]

    # Query MongoDB to get matching documents for all APIs
    selected_apis = list(apis.find({"API Name": {"$in": api_list}}))

    # Convert ObjectId to string if needed
    for doc in selected_apis:
        doc["_id"] = str(doc["_id"])


    # Generate BPMN diagram for selected Business Process
    selected_bpmn = generate_bpmn(api_list)
    
    return render_template('selected_bp.html',  
        selected_apis = selected_apis, 
        selected_bpmn = selected_bpmn,
        apis = apis_str
    )

# BPMN Viewer Route
@app.route('/select_bpmn', methods=['POST', 'GET'])
def select_bpmn():
    # generate_bpmn("hello", "bruh")

    # Get the desired business process file
    bp_file = request.files["uploaded_bp"]
    desired_bp = bp_file.read().decode('utf-8')

    # Sample business processes as the algorithm to generate programmatically has not
    # been implemented yet.
    business_processes = [
        ["UpbrainsXtract", "BabelwayTransform", "ESSValidator", "StorecoveSend"],
        ["UpbrainsXtract", "BabelwayTransform", "ESSValidator", "AdemicoSend"],
        ["UpbrainsXtract", "BabelwayTransform", "EcosioValidator", "StorecoveSend"],
        ["UpbrainsXtract", "BabelwayTransform", "EcosioValidator", "AdemicoSend"],

        ["UpbrainsXtract", "RishabhTransform", "ESSValidator", "StorecoveSend"],
        ["UpbrainsXtract", "RishabhTransform", "ESSValidator", "AdemicoSend"],
        ["UpbrainsXtract", "RishabhTransform", "EcosioValidator", "StorecoveSend"],
        ["UpbrainsXtract", "RishabhTransform", "EcosioValidator", "AdemicoSend"],

        ["EzzybillsExtract", "BabelwayTransform", "ESSValidator", "StorecoveSend"],
        ["EzzybillsExtract", "BabelwayTransform", "ESSValidator", "AdemicoSend"],
        ["EzzybillsExtract", "BabelwayTransform", "EcosioValidator", "StorecoveSend"],
        ["EzzybillsExtract", "BabelwayTransform", "EcosioValidator", "AdemicoSend"],

        ["EzzybillsExtract", "RishabhTransform", "ESSValidator", "StorecoveSend"],
        ["EzzybillsExtract", "RishabhTransform", "ESSValidator", "AdemicoSend"],
        ["EzzybillsExtract", "RishabhTransform", "EcosioValidator", "StorecoveSend"],
        ["EzzybillsExtract", "RishabhTransform", "EcosioValidator", "AdemicoSend"]
    ]

    return render_template(
        'select_bpmn.html', 
        desired_bp=desired_bp, 
        business_processes=business_processes
    )

# BPMN generator from APIs selected
def generate_bpmn(apis):

    # Helper function to format the BPMN
    def prettify(elem):
        rough_string = tostring(elem, 'utf-8')
        return minidom.parseString(rough_string).toprettyxml(indent="  ")

    # BPMN Namespaces
    NS = {
        'bpmn': "http://www.omg.org/spec/BPMN/20100524/MODEL",
        'bpmndi': "http://www.omg.org/spec/BPMN/20100524/DI",
        'omgdc': "http://www.omg.org/spec/DD/20100524/DC",
        'omgdi': "http://www.omg.org/spec/DD/20100524/DI"
    }

    def qname(ns, tag):
        return f"{{{NS[ns]}}}{tag}"


    # BPMN definitions
    defs = Element(qname('bpmn', 'definitions'), {
        'xmlns:bpmn': NS['bpmn'],
        'xmlns:bpmndi': NS['bpmndi'],
        'xmlns:omgdc': NS['omgdc'],
        'xmlns:omgdi': NS['omgdi'],
        'id': "Definitions_1",
        'targetNamespace': "http://bpmn.io/schema/bpmn"
    })

    # Process Node
    process = SubElement(defs, qname('bpmn', 'process'), {
        'id': "Process_1",
        'isExecutable': "true"
    })

    # BPMN Diagram plane
    bpmndi = SubElement(defs, qname('bpmndi', 'BPMNDiagram'), {'id': "BPMNDiagram_1"})
    plane = SubElement(bpmndi, qname('bpmndi', 'BPMNPlane'), {
        'id': "BPMNPlane_1",
        'bpmnElement': "Process_1"
    })

    # Layout constants
    start_x, start_y = 150, 80  # Lowered Y
    task_width, task_height = 100, 80
    start_radius = 36
    spacing = 200  # Shorter arrow distance than before
    y_center = start_y + task_height // 2

    # Start Event
    start_id = "StartEvent_1"
    start = SubElement(process, qname('bpmn', 'startEvent'), {
        'id': start_id,
        'name': "Start"
    })
    
    # Start event on diagram
    SubElement(plane, qname('bpmndi', 'BPMNShape'), {
        'id': f"{start_id}_di",
        'bpmnElement': start_id
    }).append(Element(qname('omgdc', 'Bounds'), {
        'x': str(start_x),
        'y': str(start_y + 22),
        'width': str(start_radius),
        'height': str(start_radius)
    }))

    prev_id = start_id
    prev_x = start_x
    flow_counter = 1
    x_pos = start_x + spacing/2

    # Progressively add each API
    for i, name in enumerate(apis):
        api_type = 'serviceTask' if i < len(apis) - 1 else 'sendTask'
        api_id = f"Activity_{i}"

        task = SubElement(process, qname('bpmn', api_type), {
            'id': api_id,
            'name': name
        })

        # Sequence flow
        flow_id = f"Flow_{flow_counter}"
        SubElement(process, qname('bpmn', 'sequenceFlow'), {
            'id': flow_id,
            'sourceRef': prev_id,
            'targetRef': api_id
        })

        edge = SubElement(plane, qname('bpmndi', 'BPMNEdge'), {
            'id': f"{flow_id}_di",
            'bpmnElement': flow_id
        })

        if prev_id == start_id:
            prev_width = start_radius
        else:
            prev_width = task_width

        edge.extend([
            Element(qname('omgdi', 'waypoint'), {'x': str(prev_x + prev_width), 'y': str(y_center)}),
            Element(qname('omgdi', 'waypoint'), {'x': str(x_pos), 'y': str(y_center)})
        ])

        SubElement(plane, qname('bpmndi', 'BPMNShape'), {
            'id': f"{api_id}_di",
            'bpmnElement': api_id
        }).append(Element(qname('omgdc', 'Bounds'), {
            'x': str(x_pos),
            'y': str(start_y),
            'width': str(task_width),
            'height': str(task_height)
        }))

        prev_id = api_id
        prev_x = x_pos
        x_pos += spacing
        flow_counter += 1

    return prettify(defs)

# Populate database with relevant API information
def populate_db():
    # API Documents for MongoDB insertion - dockerise MongoDB instance.
    api_dict = [
        {
            "API Name": "ESSValidator",
            "Business Name": "ESS",
            "Inputs": ["UBL"],
            "Category": "Validation",
            "Type of Output": "Business Rules",
            "Business Rules": [
                "AUNZ_PEPPOL_1_0_10",
                "AUNZ_PEPPOL_SB_1_0_10",
                "AUNZ_UBL_1_0_10",
                "FR_EN16931_CII_1_3_11",
                "FR_EN16931_UBL_1_3_11",
                "RO_RO16931_UBL_1_0_8_EN16931",
                "RO_RO16931_UBL_1_0_8_CIUS_RO"
            ],
            "Outputs": ["Validation Report"],
            "Description": "Validates document formats and contents against ESS rulesets.",
            "Link to company website/documentation": "https://services.ebusiness-cloud.com/ess-schematron/docs",
            "API Endpoint": "https://services.ebusiness-cloud.com/ess-schematron/v1/api/validate",
            "Instructions": "AWS Step Function setup required to use custom API endpoint.",
            "Authentication Procedures": "OAuth2",
            "Client": ["Web", "REST"],
            "Lambda Code": """ \
                import requests
                import json
                import base64
                import hashlib

                def lambda_handler(event, context):
                    
                    response = requests.post('https://dev-eat.auth.eu-central-1.amazoncognito.com/oauth2/token',
                                            data={"grant_type": "client_credentials",
                                                "client_id":"7d30bi87iptegbrf2bp37p42gg",
                                                "client_secret": "880tema3rvh3h63j4nquvgoh0lgts11n09bq8597fgrkvvd62su",
                                                "scope":"eat/read"},
                                            headers={"Content-Type": "application/x-www-form-urlencoded"})
                    
                    token = response.json()['access_token']
                    print(token)
                    
                    filename = "Invoice.xml"
                    rules = ["AUNZ_PEPPOL_1_0_10", "AUNZ_PEPPOL_SB_1_0_10", "AUNZ_UBL_1_0_10", "FR_EN16931_CII_1_3_14", "FR_EN16931_UBL_1_3_14", "RO_RO16931_UBL_1_0_8_EN16931", "RO_RO16931_UBL_1_0_8_CIUS_RO"]
                    file = event["body"]

                    bdata = file.encode("utf-8")
                    b64encoded = base64.b64encode(bdata)
                    b64estr = b64encoded.decode("utf-8")    
                    md5h = hashlib.md5(b64encoded)

                    data = json.dumps({"file":filename, "content":b64estr, "checksum":md5h.hexdigest()})
                    params = {"rules":rules}
                    headers = {"Content-Type": "application/json", "Accept-Language":"en", "Authorization":"Bearer " + token} 
                    url = "https://services.ebusiness-cloud.com/ess-schematron/v1/api/validate?"          
                    response = requests.request("POST", url[:-1], headers=headers, data=data, params=params)

                    
                    return {
                        'statusCode': 200,
                        'body': response.json()
                    }
            """,
            "Requirements": ["requests"]
        },
        {
            "API Name": "ECOSIO",
            "Business Name": "Ecosio GmbH",
            "Inputs": ["PDF", "JSON", "UBL"],
            "Category": "Validation",
            "Type of Output": "Business Rules",
            "Business Rules": [
                "EN_16931_CII",
                "EHF",
                "EN_16931_UBL Credit Note",
                "A-NZ_PEPPOL_BIS3",
                "OpenPEPPOL formats",
                "CII Cross Industry formats",
                "OIOUBL"
            ],
            "Outputs": ["Validation Report"],
            "Description": "Validates electronic documents in compliance with European and international standards.",
            "Link to company website/documentation": "https://ecosio.com/en/",
            "API Endpoint": "https://api.ecosio.sample/validate",
            "Instructions": "AWS Step Function setup required to use custom API endpoint.",
            "Authentication Procedures": "OAuth2",
            "Client": ["Web", "REST"]
        },
        {
            "API Name": "StorecoveSend",
            "Business Name": "Storecove",
            "Inputs": ["PDF", "JSON", "UBL"],
            "Category": "Communication",
            "Type of Output": "Network",
            "Network": "AU PEPPOL",
            "Description": "Sends electronic documents across compliant networks including AU PEPPOL.",
            "Link to company website/documentation": "https://storecove.com",
            "API Endpoint": "https://api.storecove.fake/send",
            "Instructions": "AWS Step Function setup required to use custom API endpoint.",
            "Authentication Procedures": "Token",
            "Client": ["Web", "REST"],
            "Lambda Code": """ \
                
            """,
            "Requirements": ["requests"]
        },
        {
            "API Name": "AdemicoSend",
            "Business Name": "Ademico",
            "Inputs": ["PDF", "JSON", "UBL"],
            "Category": "Communication",
            "Type of Output": "Network",
            "Network": "AU PEPPOL",
            "Description": "Securely sends invoices and business documents via AU PEPPOL network.",
            "Link to company website/documentation": "https://ademico.example.com/docs",
            "API Endpoint": "https://api.ademico.sample/send",
            "Instructions": "AWS Step Function setup required to use custom API endpoint.",
            "Authentication Procedures": "OAuth2",
            "Client": ["REST"]
        },
        {
            "API Name": "UpbrainsXtract",
            "Business Name": "Upbrains",
            "Inputs": ["PDF", "JSON", "UBL"],
            "Category": "Transformation",
            "Type of Output": "Output",
            "Outputs": ["JSON", "XML"],
            "Description": "Extracts structured data from unstructured business documents.",
            "Link to company website/documentation": "https://upbrains.example.com/docs",
            "API Endpoint": "https://workflow.upbrainsai.com/api/v1/webhooks/yS0k3e9RAQfESRDWUQskz",
            "Instructions": "AWS Step Function setup required to use custom API endpoint.",
            "Authentication Procedures": "Token",
            "Client": ["Web", "Command Line", "REST"],
            "Lambda Code": """ \
                import requests
                import json
                import base64
                import hashlib
                import os
                import pre
                import boto3

                def lambda_handler(event, context):

                    s3 = boto3.client('s3')    
                    
                    token = os.environ['token']
                    workflowtoken = os.environ['workflowtoken']

                    bucket = event['bucket']
                    key = event['key']
                    destination_bucket = bucket
                    destination_key = key + "-upbrains"    

                    file = s3.get_object(Bucket=event['bucket'], Key=event['key'])['Body'].read()

                    url = "https://workflow.upbrainsai.com/api/v1/webhooks/yS0k3e9RAQfESRDWUQskz" # "https://xtract.upbrainsai.com/api/invoice"

                    payload = {
                        'file': file 
                    }

                    # files= [('file',(filename,file,'application/pdf'))]

                    headers = {
                        'Authorization': "Bearer " + workflowtoken,    
                        'Content-Type': 'application/json'
                    } 

                    # response = requests.request("POST", url, headers=headers, json=payload)#, files=files) 
                    # print(response.text)

                    s3.put_object(Bucket=destination_bucket, Key=destination_key, Body=pre.pre, ContentType = "application/json")

                    info = {
                        "bucket": destination_bucket,
                        "key": destination_key
                    }
                    
                    return   {
                        'statusCode': 200,
                        'body': json.dumps(info)
                    }
            """,
            "Requirements": ["requests"]
        },
        {
            "API Name": "EzzyBillsExtract",
            "Business Name": "EzzyBills",
            "Inputs": ["PDF", "JSON", "UBL"],
            "Category": "Transformation",
            "Type of Output": "Output",
            "Outputs": ["JSON", "XML"],
            "Description": "Performs intelligent invoice extraction and returns JSON/XML formats.",
            "Link to company website/documentation": "https://www.ezzybills.com",
            "API Endpoint": "https://api.ezzybills.sample/extract",
            "Instructions": "AWS Step Function setup required to use custom API endpoint.",
            "Authentication Procedures": "Token",
            "Client": ["Web", "REST"]
        },
        {
            "API Name": "TradeshiftTransform",
            "Business Name": "Tradeshift",
            "Inputs": ["PDF", "JSON", "UBL"],
            "Category": "Transformation",
            "Type of Output": "Output",
            "Outputs": ["JSON", "XML"],
            "Description": "Transforms business documents into globally compliant formats.",
            "Link to company website/documentation": "https://tradeshift.com",
            "API Endpoint": "https://api.tradeshift.sample/transform",
            "Instructions": "AWS Step Function setup required to use custom API endpoint.",
            "Authentication Procedures": "OAuth2",
            "Client": ["Web", "REST"]
        },
        {
            "API Name": "RishabhTransform",
            "Business Name": "Rishabh Software",
            "Inputs": ["PDF", "JSON", "UBL"],
            "Category": "Transformation",
            "Type of Output": "Output",
            "Outputs": ["JSON", "XML"],
            "Description": "Offers customized document transformation services for business workflows.",
            "Link to company website/documentation": "https://rishabh.example.com/docs",
            "API Endpoint": "NIL",
            "Instructions": "AWS Step Function setup required to use custom API endpoint.",
            "Authentication Procedures": "OAuth2",
            "Client": ["REST", "Web"],
            "Lambda Code": """ \
                
            """,
            "Requirements": ["requests", "lxml"]
        }
    ]
    
    business_rules = [
        {"Type": "AUNZ_PEPPOL_1_0_10", "API": "ESSValidator"},
        {"Type": "AUNZ_PEPPOL_SB_1_0_10", "API": "ESSValidator"},
        {"Type": "AUNZ_UBL_1_0_10", "API": "ESSValidator"},
        {"Type": "FR_EN16931_CII_1_3_14", "API": "ESSValidator"},
        {"Type": "FR_EN16931_UBL_1_3_14", "API": "ESSValidator"},
        {"Type": "RO_RO16931_UBL_1_0_8_EN16931", "API": "ESSValidator"},
        {"Type": "RO_RO16931_UBL_1_0_8_CIUS_RO", "API": "ESSValidator"},
        {"Type": "EN_16931_CII", "API": "ECOSIO"},
        {"Type": "EHF", "API": "ECOSIO"},
        {"Type": "EN_16931_UBL Credit Note", "API": "ECOSIO"},
        {"Type": "A-NZ_PEPPOL_BIS3", "API": "ECOSIO"},
        {"Type": "OpenPEPPOL formats", "API": "ECOSIO"},
        {"Type": "CII Cross Industry formats", "API": "ECOSIO"},
        {"Type": "OIOUBL", "API": "ECOSIO"}
    ]
    
    apis.insert_many(api_dict)
    inputs.insert_many([{"Type": "JSON"}, {"Type": "PDF"}, {"Type": "XML_UBL"}])
    outputs.insert_many([{"Type": "JSON_UB"}, {"Type": "JSON_EB"}, {"Type": "JSON_TS"}, {"Type": "XML_UBL"}, {"Type": "PDF"}])
    brs.insert_many(business_rules)

# Main function
if __name__ == '__main__':
    client.drop_database(db)
    populate_db()

    app.run(debug=True, port=8000, use_reloader=False)
